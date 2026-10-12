"""Detect language (EN/IT) of a pasted text, produce a native rewrite at two registers
(colloquial and soutenu) plus a critique classified against the idiolecte taxonomy, and
(if corrections are found) update the matching idiolecte profile in the Obsidian vault
directly (ADR 0018 - no human review before writing).

CLI helper for the Language Coach window (issue #19, tickets #20/#21/#22, register split
added afterwards). Reads a text (file or stdin), calls Ollama cloud with both language
taxonomies as context, parses a strict delimited response format, writes a human-readable
preview for the AHK window, and mutates the idiolecte.md of the detected language.

Exit code != 0 on any failure - an empty output file is never passed off as success.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
from collections import Counter
from datetime import date as _date
from pathlib import Path

from prompt_rewrite import _fail, _section, cloud_chunks, openai_chunks

DEFAULT_RUBRIC = Path(__file__).resolve().parent / "language_coach_rubric.md"
# Le vault Obsidian ou vivent profils d'idiolecte et classes d'erreurs.
# Surchargeable par LANGUAGE_COACH_VAULT ou par --vault-dir.
_VAULT_REL = Path("Noyau") / "Coaches" / "Coach Tekhnè" / "langues"
DEFAULT_VAULT_DIR = Path(os.environ.get("LANGUAGE_COACH_VAULT") or Path.home() / "Obsidian" / "vault") / _VAULT_REL
LANG_DIR = {"en": "anglais", "it": "italien"}

LANG_RE = re.compile(r"^LANG:\s*(en|it)\s*$", re.M | re.I)
COLLOQUIAL_MARK = "===NATIF_COLLOQUIAL==="
SOUTENU_MARK = "===NATIF_SOUTENU==="
CORR_MARK = "===CORRECTIONS==="

CLASS_HEADER_RE = re.compile(r"^### ([A-Z]) [—–-] (.+)$", re.M)
FREQ_RE = re.compile(r"(-\s*\*\*Fr[ée]quence\*\*\s*:\s*)(\d+)")
EXEMPLES_RE = re.compile(r"(-\s*\*\*Exemples\*\*\s*:\s*\n(?:  - .*\n)*)")
METRIQUE_HEADING = "## Métrique de progrès global"
TABLE_HEADER = "| Date | Doc | Mots | Interventions | /100 mots | Classe dominante |"
JOURNAL_HEADING = "## Journal"
EMPTY_PLACEHOLDER_RE = re.compile(r"_Aucune classe pour l'instant.*?_\n\n", re.S)

MIN_CORRECTION_FIELDS = 5   # letter + original + suggestion_colloquiale + suggestion_soutenue + idiome


def idiolecte_path(vault_dir: Path, lang: str) -> Path:
    return vault_dir / LANG_DIR[lang] / "idiolecte.md"


def build_system_prompt(rubric: str, en_classes: str, it_classes: str,
                        contexte: str = "") -> str:
    parties = [
        rubric.strip(),
        "\n## Taxonomie anglais existante\n" + (en_classes.strip() or "(aucune classe existante)"),
        "\n## Taxonomie italien existante\n" + (it_classes.strip() or "(aucune classe existante)"),
    ]
    if contexte.strip():
        # Le contexte sert a lever les ambiguites de domaine (jargon, noms
        # propres, sujet en cours). Il ne doit JAMAIS etre corrige ni analyse :
        # seul le texte soumis l'est.
        parties.append(
            "\n## Contexte de la session en cours (information seulement)\n"
            "Ce qui suit est la conversation en cours dans l'editeur de l'utilisateur.\n"
            "Sers-t'en pour comprendre le domaine, le jargon et les noms propres.\n"
            "N'analyse PAS ce contexte et n'en corrige RIEN : seul le texte soumis\n"
            "par l'utilisateur doit etre critique.\n\n"
            + contexte.strip()
        )
    return "\n".join(parties)


def _recover_fields(fields: list[str]) -> list[str] | None:
    """Rattraper une ligne ou le modele a double les separateurs.

    Le contrat est de 8 champs (lettre|label|tendance|strategie|original|
    colloquiale|soutenue|idiome). En pratique le modele emet parfois `||` a
    partir du 5e champ, ce qui produit 12 champs dont 5 vides et decale tout
    le decoupage positionnel -> la correction etait perdue.

    On recompacte en retirant les champs vides (la lettre reste en tete), puis
    on interprete selon le nombre de champs utiles restants. Renvoie None si
    aucune lecture n'est certaine, plutot que de deviner.
    """
    compact = [fields[0]] + [f for f in fields[1:] if f]
    n = len(compact)
    if n == 8:                       # classe neuve, idiome fourni
        return compact
    if n == 7:                       # classe neuve, idiome absent
        return compact + [""]
    if n == 5:                       # classe existante, idiome fourni
        l, orig, coll, sout, idio = compact
        return [l, "", "", "", orig, coll, sout, idio]
    if n == 4:                       # classe existante, idiome absent
        l, orig, coll, sout = compact
        return [l, "", "", "", orig, coll, sout, ""]
    return None


def parse_model_output(text: str) -> dict:
    """Strict parse of the LANG / ===NATIF_COLLOQUIAL=== / ===NATIF_SOUTENU=== /
    ===CORRECTIONS=== contract. Raises ValueError on any structural failure (never
    guesses a language or a fragment)."""
    m = LANG_RE.search(text)
    if not m:
        raise ValueError("marqueur LANG manquant ou langue non reconnue (attendu en/it)")
    lang = m.group(1).lower()
    if COLLOQUIAL_MARK not in text or SOUTENU_MARK not in text or CORR_MARK not in text:
        raise ValueError(
            "marqueurs ===NATIF_COLLOQUIAL=== / ===NATIF_SOUTENU=== / ===CORRECTIONS=== manquants"
        )
    _, rest = text.split(COLLOQUIAL_MARK, 1)
    colloquial_part, rest = rest.split(SOUTENU_MARK, 1)
    soutenu_part, corr_part = rest.split(CORR_MARK, 1)
    native_colloquial = colloquial_part.strip()
    native_soutenu = soutenu_part.strip()
    if not native_colloquial or not native_soutenu:
        raise ValueError("version native colloquiale ou soutenue vide")

    corrections = []
    dropped = []
    for line in corr_part.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("```"):
            # deepseek-v4.1-flash entoure sa reponse d'un bloc ``` puis commente
            # apres : la cloture termine la reponse, la suite n'est pas une perte.
            # Un ``` avant toute correction est une ouverture : on la saute.
            if corrections or dropped:
                break
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) > 8:
            recovered = _recover_fields(fields)
            if recovered is not None:
                fields = recovered
        if len(fields) < MIN_CORRECTION_FIELDS:
            sys.stderr.write(f"WARN\tligne de correction malformee ignoree: {line!r}\n")
            dropped.append(line)
            continue
        # Le modele est incoherent sur le nombre de champs vides du milieu (mesure en
        # reel : des separateurs "vides" se fusionnent). Seuls letter (debut) et
        # original/colloquiale/soutenue/idiome (fin, positions fixes) sont fiables ;
        # label/tendance/strategie sont lus dans ce qu'il reste au milieu.
        letter = fields[0]
        idiome = fields[-1]
        suggestion_soutenue = fields[-2]
        suggestion_colloquiale = fields[-3]
        original = fields[-4]
        label, tendance, strategie = (fields[1:-4] + ["", "", ""])[:3]
        if not re.fullmatch(r"[A-Z?]", letter):
            sys.stderr.write(f"WARN\tclasse invalide ignoree: {line!r}\n")
            dropped.append(line)
            continue
        if not original or not suggestion_colloquiale or not suggestion_soutenue:
            sys.stderr.write(f"WARN\tcorrection incomplete ignoree: {line!r}\n")
            dropped.append(line)
            continue
        corrections.append({
            "letter": letter, "label": label, "tendance": tendance, "strategie": strategie,
            "original": original,
            "suggestion_colloquiale": suggestion_colloquiale,
            "suggestion_soutenue": suggestion_soutenue,
            "idiome": idiome,
        })
    return {
        "lang": lang,
        "native_colloquial": native_colloquial,
        "native_soutenu": native_soutenu,
        "corrections": corrections,
        "dropped": dropped,
    }


def format_preview(parsed: dict) -> str:
    lines = [
        f"Langue detectee : {parsed['lang']}", "",
        "-- Version colloquiale --", parsed["native_colloquial"], "",
        "-- Version soutenue --", parsed["native_soutenu"], "",
    ]
    if parsed["corrections"]:
        lines.append("-- Corrections --")
        for c in parsed["corrections"]:
            tag = c["label"] or "classe existante"
            lines.append(
                f"[{c['letter']}] {tag} : \"{c['original']}\" "
                f"-> colloquial: \"{c['suggestion_colloquiale']}\" "
                f"/ soutenu: \"{c['suggestion_soutenue']}\""
            )
            if c["idiome"]:
                lines.append(f"    idiome plus frequent : \"{c['idiome']}\"")
    else:
        lines.append("Aucune correction : texte deja natif dans les 2 registres.")
    # Rendre les pertes visibles : sans ca, une correction rejetee disparaissait
    # sans trace pour l'appelant (l'overlay Omarchy n'aurait rien affiche).
    n = len(parsed.get("dropped") or ())
    if n:
        lines += ["", f"/!\\ {n} correction(s) illisible(s), non appliquee(s) :"]
        lines += [f"    {ligne[:110]}" for ligne in parsed["dropped"]]
    return "\n".join(lines)


def _class_sections(content: str):
    """(letter, label, start, end) for each '### <letter> — <label>' block, bounded by
    the next '### ' or the next '## ' heading - whichever comes first."""
    matches = list(CLASS_HEADER_RE.finditer(content))
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        next_h2 = content.find("\n## ", start)
        if next_h2 != -1 and next_h2 < end:
            end = next_h2
        out.append((m.group(1), m.group(2), start, end))
    return out


def _correction_bullets(correction: dict) -> str:
    """Two bullets (colloquial + soutenu) always, plus a 3rd bullet for the idiome
    when present. Colloquial/soutenu each count as one occurrence in Frequence (see
    apply_correction) - the idiome bullet is illustrative only, never counted (it is
    not a distinct observed mistake)."""
    original = correction["original"]
    bullets = (
        f"  - `{original}` → `{correction['suggestion_colloquiale']}` (colloquial)\n"
        f"  - `{original}` → `{correction['suggestion_soutenue']}` (soutenu)\n"
    )
    if correction["idiome"]:
        bullets += f"  - `{original}` → `{correction['idiome']}` (idiome plus frequent)\n"
    return bullets


def _bump_class(content: str, start: int, end: int, correction: dict) -> str:
    section = content[start:end]
    freq_m = FREQ_RE.search(section)
    if freq_m:
        # +2, pas +1 : chaque correction porte desormais 2 registres (colloquial +
        # soutenu), chacun compte comme une occurrence distincte (choix explicite,
        # cf. conversation qui a etendu ADR 0018 - pas une nouvelle ADR separee).
        new_n = int(freq_m.group(2)) + 2
        section = section[:freq_m.start()] + freq_m.group(1) + str(new_n) + section[freq_m.end():]
    bullets = _correction_bullets(correction)
    ex_m = EXEMPLES_RE.search(section)
    if ex_m:
        insert_at = ex_m.end()
        section = section[:insert_at] + bullets + section[insert_at:]
    else:
        section = section.rstrip("\n") + f"\n- **Exemples** :\n{bullets}"
    return content[:start] + section + content[end:]


def _create_class(content: str, correction: dict) -> str:
    if METRIQUE_HEADING not in content:
        raise ValueError(f"section '{METRIQUE_HEADING}' introuvable, gabarit idiolecte inattendu")
    letter = correction["letter"]
    bullets = _correction_bullets(correction)
    block = (
        f"\n### {letter} — {correction['label']}\n"
        f"- **Tendance** : {correction['tendance']}\n"
        f"- **Stratégie native** : {correction['strategie']}\n"
        f"- **Exemples** :\n"
        f"{bullets}"
        f"- **Fréquence** : 2\n"
    )
    return content.replace(METRIQUE_HEADING, block + "\n" + METRIQUE_HEADING, 1)


def _resolve_unknown_letter(sections, label: str) -> str:
    """Letter of the class with the same label, else the next free letter."""
    wanted = label.strip().casefold()
    for l, existing, _start, _end in sections:
        if wanted and existing.strip().casefold() == wanted:
            return l
    used = [l for l, *_ in sections]
    return chr(ord(max(used)) + 1) if used else "A"


def apply_correction(content: str, correction: dict) -> str:
    """Bump an existing class letter, or create a new one from the model's proposed
    label/tendance/strategie. A letter that collides with an existing class always wins
    as a bump (the existing curated label is kept, the model's proposed one is dropped)
    - see ADR 0018: never overwrite hand-curated prose, only append/increment.
    ponytail: markdown surgery by regex, tied to the exact heading/table text of the
    idiolecte gabarit (tickets #21/#1). A gabarit rewrite breaks this loudly (ValueError
    on the missing heading), not silently."""
    letter = correction["letter"]
    sections = _class_sections(content)
    if letter == "?":
        letter = _resolve_unknown_letter(sections, correction["label"])
        correction = {**correction, "letter": letter}
    for l, _label, start, end in sections:
        if l == letter:
            return _bump_class(content, start, end, correction)
    if not correction["label"] or not correction["tendance"] or not correction["strategie"]:
        sys.stderr.write(
            f"WARN\tclasse {letter} inconnue et incomplete pour creation, correction ignoree\n"
        )
        return content
    return _create_class(content, correction)


def append_metric_row(content: str, date: str, doc: str, words: int, n: int, dominant: str) -> str:
    idx = content.find(TABLE_HEADER)
    if idx == -1:
        raise ValueError("tableau de metrique introuvable, gabarit idiolecte inattendu")
    pos = idx + len(TABLE_HEADER)
    nl = content.index("\n", pos) + 1
    end = content.index("\n", nl) + 1          # end of the '|---|...' separator line
    while content[end:end + 1] == "|":
        end = content.index("\n", end) + 1
    per100 = round(n / words * 100, 1) if words else 0.0
    row = f"| {date} | {doc} | ~{words} | {n} | {per100} | {dominant} |\n"
    return content[:end] + row + content[end:]


def append_journal_entry(content: str, entry: str) -> str:
    if JOURNAL_HEADING not in content:
        raise ValueError(f"section '{JOURNAL_HEADING}' introuvable, gabarit idiolecte inattendu")
    return content.rstrip("\n") + "\n" + entry + "\n"


def _profile_lock(path: Path) -> Path:
    """Fichier de verrou propre a un profil, hors du vault pour ne pas etre synchronise."""
    key = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"language-coach-{key}.lock"


def update_idiolecte(path: Path, corrections: list[dict], word_count: int,
                      date: str, doc_label: str) -> str:
    """Lecture-modification-ecriture du profil sous verrou exclusif : le panneau et les
    imports de Claude tournent en meme temps, et sans verrou une ecriture en ecrasait
    une autre (corrections perdues ou doublees, ligne de journal tronquee)."""
    with open(_profile_lock(path), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _update_idiolecte_locked(path, corrections, word_count, date, doc_label)


def _update_idiolecte_locked(path: Path, corrections: list[dict], word_count: int,
                             date: str, doc_label: str) -> str:
    """Mutate idiolecte.md in place. Returns the dominant class letter, or '' if no
    correction actually applied (all incomplete-new-class, or empty list). The
    "Interventions" count in the metric row is the number of DISTINCT corrections
    found (not doubled) - it measures the error rate, not the example count; only
    each class's Frequence counts +2 per correction (see apply_correction)."""
    raw = path.read_bytes()
    # Le vault vient de Windows : ses fichiers sont en CRLF. On mémorise la
    # convention du fichier pour la restaurer à l'écriture, sinon chaque passage
    # réécrirait les ~150 lignes et ferait apparaître le fichier entier comme
    # modifié dans Dropbox, Obsidian et git.
    newline = "\r\n" if b"\r\n" in raw else "\n"
    content = raw.decode("utf-8").replace("\r\n", "\n")
    applied = []
    for c in corrections:
        if c["letter"] == "?":
            c = {**c, "letter": _resolve_unknown_letter(_class_sections(content), c["label"])}
        before = content
        content = apply_correction(content, c)
        if content != before:
            applied.append(c["letter"])
    if not applied:
        return ""
    content = EMPTY_PLACEHOLDER_RE.sub("", content, count=1)
    dominant = Counter(applied).most_common(1)[0][0]
    content = append_metric_row(content, date, doc_label, word_count, len(applied), dominant)
    entry = (
        f"- {date} — correction automatique (language_coach) : {len(applied)} "
        f"intervention(s), classe(s) {', '.join(sorted(set(applied)))}."
    )
    content = append_journal_entry(content, entry)
    # Ecriture atomique : Obsidian ou Dropbox ne lisent jamais un fichier a moitie ecrit.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(content.replace("\n", newline).encode("utf-8"))
    os.replace(tmp, path)
    return dominant


def read_text(text_file: str | None) -> str:
    if text_file:
        return Path(text_file).read_text(encoding="utf-8")
    return sys.stdin.read()


STUB_RESPONSE = (
    "LANG: en\n"
    "===NATIF_COLLOQUIAL===\n[STUB] colloquial rewrite of the input.\n"
    "===NATIF_SOUTENU===\n[STUB] formal rewrite of the input.\n"
    "===CORRECTIONS===\n"
)


def parse_think(value: str):
    """--think -> valeur du champ `think` d'Ollama ('' = ne pas l'envoyer)."""
    v = value.strip().lower()
    if not v:
        return None
    return {"false": False, "true": True}.get(v, v)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-file", help="texte a corriger; stdin si omis")
    parser.add_argument("--model", default="gpt-oss:120b", help="cloud model name")
    parser.add_argument("--think", default="",
                        help="niveau de reflexion : low|medium|high (gpt-oss), true|false ; "
                             "vide = defaut du modele")
    parser.add_argument("--output", required=True, help="fichier de preview (pour l'AHK)")
    parser.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    parser.add_argument("--host", default="https://ollama.com")
    parser.add_argument("--provider", default="ollama", choices=["ollama", "groq"],
                        help="ollama (cloud, --host) ou groq (API OpenAI, cle GROQ_API_KEY)")
    parser.add_argument("--vault-dir", default=str(DEFAULT_VAULT_DIR),
                         help="dossier langues/ du vault (override pour les tests)")
    parser.add_argument("--doc-label", default="language_coach (ad hoc)")
    parser.add_argument("--date", help="date ISO pour le journal/metrique (override pour les tests)")
    parser.add_argument("--error-file")
    parser.add_argument("--stub", action="store_true", help="no-network echo mode")
    parser.add_argument("--response-file",
                        help="reponse deja redigee au format du contrat (par Claude) : "
                             "aucun appel au modele")
    parser.add_argument("--context-file",
                        help="contexte de session a donner au modele (jamais corrige)")
    parser.add_argument("--stream", action="store_true",
                        help="ecrire chaque morceau de reponse sur stdout, une ligne "
                             "JSON {\"delta\": ...} par morceau (affichage en direct)")
    parser.add_argument("--dry-run", action="store_true",
                        help="produire la critique sans jamais ecrire dans le profil")
    args = parser.parse_args(argv)

    err = args.error_file
    if err:
        try:
            Path(err).unlink()
        except OSError:
            pass

    rubric_path = Path(args.rubric)
    if not rubric_path.is_file():
        return _fail(f"rubrique introuvable: {rubric_path}", err)
    rubric = rubric_path.read_text(encoding="utf-8")

    try:
        text = read_text(args.text_file)
    except OSError as e:
        return _fail(f"texte illisible: {e}", err)
    if not text.strip():
        return _fail("texte vide", err)

    vault_dir = Path(args.vault_dir)
    en_path = idiolecte_path(vault_dir, "en")
    it_path = idiolecte_path(vault_dir, "it")
    en_classes = _section(en_path.read_text(encoding="utf-8"), "Classes d'erreurs actives") if en_path.is_file() else ""
    it_classes = _section(it_path.read_text(encoding="utf-8"), "Classes d'erreurs actives") if it_path.is_file() else ""
    contexte = ""
    if args.context_file:
        try:
            contexte = Path(args.context_file).read_text(encoding="utf-8")
        except OSError:
            contexte = ""      # un contexte absent ne doit jamais bloquer l'analyse
    system = build_system_prompt(rubric, en_classes, it_classes, contexte)

    if args.response_file:
        try:
            raw = Path(args.response_file).read_text(encoding="utf-8")
        except OSError as e:
            return _fail(f"reponse illisible: {e}", err)
    elif args.stub:
        raw = STUB_RESPONSE
    else:
        try:
            morceaux = []
            if args.provider == "ollama":
                flux = cloud_chunks(args.model, system, text, args.host,
                                    think=parse_think(args.think))
            else:
                flux = openai_chunks(args.model, system, text, args.provider,
                                     think=parse_think(args.think))
            for piece in flux:
                morceaux.append(piece)
                if args.stream:
                    print(json.dumps({"delta": piece}, ensure_ascii=False), flush=True)
            raw = "".join(morceaux)
        except (urllib.error.URLError, RuntimeError, ValueError, OSError) as e:
            return _fail(f"echec appel modele: {type(e).__name__}: {e}", err)
    if not raw.strip():
        return _fail("reponse vide du modele", err)

    try:
        parsed = parse_model_output(raw)
    except ValueError as e:
        return _fail(f"sortie du modele non conforme: {e}", err)

    preview = format_preview(parsed)
    if args.dry_run:
        preview = ("[DRY-RUN] le profil d'idiolecte n'a PAS ete modifie.\n\n" + preview)
    Path(args.output).write_text(preview, encoding="utf-8")

    if parsed["corrections"] and not args.dry_run:
        target = idiolecte_path(vault_dir, parsed["lang"])
        if not target.is_file():
            return _fail(f"profil idiolecte introuvable: {target}", err)
        date_str = args.date or _date.today().isoformat()
        update_idiolecte(target, parsed["corrections"], len(text.split()), date_str, args.doc_label)

    if parsed.get("dropped"):
        # Code 2 : le traitement a abouti mais des corrections ont ete perdues.
        # Distinct de 1 (echec franc) pour que l'appelant puisse nuancer.
        sys.stderr.write(
            f"WARN\t{len(parsed['dropped'])} correction(s) non appliquee(s) "
            "(format de ligne illisible)\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
