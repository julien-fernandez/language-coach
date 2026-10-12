import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "language_coach", Path(__file__).resolve().parents[1] / "language_coach.py"
)
language_coach = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(language_coach)


# --- parse_model_output ------------------------------------------------------------

def test_parse_model_output_no_corrections():
    raw = (
        "LANG: en\n"
        "===NATIF_COLLOQUIAL===\nAll good already, colloquial.\n"
        "===NATIF_SOUTENU===\nAll good already, formal.\n"
        "===CORRECTIONS===\n"
    )
    parsed = language_coach.parse_model_output(raw)
    assert parsed == {
        "lang": "en",
        "native_colloquial": "All good already, colloquial.",
        "native_soutenu": "All good already, formal.",
        "corrections": [],
        "dropped": [],          # ajoute par l'issue 003
    }


def test_parse_model_output_existing_class_with_idiome():
    raw = (
        "LANG: en\n"
        "===NATIF_COLLOQUIAL===\nThis meeting was awesome.\n"
        "===NATIF_SOUTENU===\nThis meeting was incredibly fruitful.\n"
        "===CORRECTIONS===\n"
        "B|||especially rich|awesome|incredibly fruitful|it was a real game changer\n"
    )
    parsed = language_coach.parse_model_output(raw)
    assert len(parsed["corrections"]) == 1
    c = parsed["corrections"][0]
    assert c["letter"] == "B"
    assert c["label"] == "" and c["tendance"] == "" and c["strategie"] == ""
    assert c["original"] == "especially rich"
    assert c["suggestion_colloquiale"] == "awesome"
    assert c["suggestion_soutenue"] == "incredibly fruitful"
    assert c["idiome"] == "it was a real game changer"


def test_parse_model_output_new_class_no_idiome():
    raw = (
        "LANG: it\n"
        "===NATIF_COLLOQUIAL===\nTutto ok.\n"
        "===NATIF_SOUTENU===\nTutto regolare.\n"
        "===CORRECTIONS===\n"
        "F|Adjectifs calques|traduit mot a mot|chercher l'idiomatique|very good|fico|eccellente|\n"
    )
    parsed = language_coach.parse_model_output(raw)
    c = parsed["corrections"][0]
    assert c["letter"] == "F" and c["label"] == "Adjectifs calques"
    assert c["suggestion_colloquiale"] == "fico" and c["suggestion_soutenue"] == "eccellente"
    assert c["idiome"] == ""


def test_parse_model_output_malformed_lines_skipped_not_fatal(capsys):
    raw = (
        "LANG: it\n"
        "===NATIF_COLLOQUIAL===\nVa tutto bene.\n"
        "===NATIF_SOUTENU===\nVa tutto bene.\n"
        "===CORRECTIONS===\n"
        "ligne pas du tout dans le format\n"
        "A||||originale|colloq|soutenu|\n"
    )
    parsed = language_coach.parse_model_output(raw)
    assert len(parsed["corrections"]) == 1
    assert parsed["corrections"][0]["letter"] == "A"
    assert "WARN" in capsys.readouterr().err


def test_parse_model_output_missing_lang_raises():
    raw = "===NATIF_COLLOQUIAL===\nx\n===NATIF_SOUTENU===\nx\n===CORRECTIONS===\n"
    try:
        language_coach.parse_model_output(raw)
        assert False, "devait lever ValueError"
    except ValueError as e:
        assert "LANG" in str(e)


def test_parse_model_output_missing_markers_raises():
    raw = "LANG: en\njuste du texte sans marqueurs\n"
    try:
        language_coach.parse_model_output(raw)
        assert False, "devait lever ValueError"
    except ValueError as e:
        assert "marqueurs" in str(e)


def test_parse_model_output_empty_native_raises():
    raw = "LANG: en\n===NATIF_COLLOQUIAL===\n   \n===NATIF_SOUTENU===\nx\n===CORRECTIONS===\n"
    try:
        language_coach.parse_model_output(raw)
        assert False, "devait lever ValueError"
    except ValueError as e:
        assert "native" in str(e)


# --- idiolecte write-back -----------------------------------------------------------

FIXTURE = """---
title: fixture
lang: en
---

# Empreinte

## Classes d'erreurs actives

### B — Faux-amis de registre
- **Tendance** : je choisis le mot le plus proche du francais.
- **Stratégie native** : preferer le mot anglais courant.
- **Exemples** :
  - `especially rich` → `incredibly fruitful` (soutenu)
- **Fréquence** : 6 (classe la plus chargee)

## Métrique de progrès global

| Date | Doc | Mots | Interventions | /100 mots | Classe dominante |
|---|---|---|---|---|---|
| 2026-06-24 | baseline | 520 | 9 | 1.7 | B |

## Exemplars natifs

- exemple existant

## Journal

- 2026-06-24 — profil initialise.
"""


def _write_fixture(tmp_path):
    p = tmp_path / "idiolecte.md"
    p.write_text(FIXTURE, encoding="utf-8")
    return p


def _correction(**overrides):
    base = {
        "letter": "B", "label": "", "tendance": "", "strategie": "",
        "original": "very interesting", "suggestion_colloquiale": "cool",
        "suggestion_soutenue": "deeply insightful", "idiome": "",
    }
    base.update(overrides)
    return base


def test_apply_correction_bumps_existing_class_by_2_and_appends_both_registers():
    out = language_coach.apply_correction(FIXTURE, _correction())
    assert "- **Fréquence** : 8 (classe la plus chargee)" in out          # 6 + 2
    assert "`very interesting` → `cool` (colloquial)" in out
    assert "`very interesting` → `deeply insightful` (soutenu)" in out
    assert "`especially rich` → `incredibly fruitful` (soutenu)" in out   # ancien exemple conserve
    assert out.count("### B —") == 1


def test_apply_correction_appends_idiome_bullet_when_present():
    out = language_coach.apply_correction(FIXTURE, _correction(idiome="a real game changer"))
    assert "`very interesting` → `a real game changer` (idiome plus frequent)" in out
    assert "- **Fréquence** : 8" in out          # l'idiome ne compte pas en plus


def test_apply_correction_creates_new_class_with_frequence_2_without_losing_sections():
    correction = _correction(
        letter="F", label="Adjectifs calques", tendance="traduit mot a mot",
        strategie="chercher l'idiomatique", original="very good",
        suggestion_colloquiale="awesome", suggestion_soutenue="excellent",
    )
    out = language_coach.apply_correction(FIXTURE, correction)
    assert "### F — Adjectifs calques" in out
    assert "- **Fréquence** : 2" in out
    assert "`very good` → `awesome` (colloquial)" in out
    assert "`very good` → `excellent` (soutenu)" in out
    assert "### B — Faux-amis de registre" in out           # section existante intacte
    assert "## Exemplars natifs" in out and "exemple existant" in out
    assert "## Journal" in out and "profil initialise" in out


def test_apply_correction_incomplete_new_class_is_skipped(capsys):
    correction = _correction(letter="F", label="", tendance="", strategie="",
                              original="x", suggestion_colloquiale="y", suggestion_soutenue="z")
    out = language_coach.apply_correction(FIXTURE, correction)
    assert out == FIXTURE
    assert "WARN" in capsys.readouterr().err


def test_update_idiolecte_writes_metric_row_and_journal(tmp_path):
    path = _write_fixture(tmp_path)
    corrections = [_correction(), _correction(original="quite big",
                                                suggestion_colloquiale="huge",
                                                suggestion_soutenue="substantial")]
    dominant = language_coach.update_idiolecte(path, corrections, word_count=40,
                                                date="2026-09-19", doc_label="test")
    assert dominant == "B"
    out = path.read_text(encoding="utf-8")
    assert "- **Fréquence** : 10 (classe la plus chargee)" in out    # 6 + 2 + 2
    assert "| 2026-09-19 | test | ~40 | 2 | 5.0 | B |" in out         # Interventions = 2, pas 4
    assert "- 2026-09-19 — correction automatique (language_coach) : 2 intervention(s), classe(s) B." in out
    assert "| 2026-06-24 | baseline | 520 | 9 | 1.7 | B |" in out     # ancienne ligne conservee


def test_update_idiolecte_strips_empty_placeholder_on_first_class(tmp_path):
    p = tmp_path / "idiolecte.md"
    p.write_text(
        "## Classes d'erreurs actives\n\n"
        "_Aucune classe pour l'instant — remplie automatiquement par `language_coach` (ADR 0018) et par le\n"
        "skill `madrelingua`, au fil des premieres corrections._\n\n"
        "## Métrique de progrès global\n\n"
        "| Date | Doc | Mots | Interventions | /100 mots | Classe dominante |\n"
        "|---|---|---|---|---|---|\n\n"
        "## Journal\n\n"
        "- init.\n",
        encoding="utf-8",
    )
    correction = _correction(letter="A", label="Calco", tendance="t", strategie="s",
                              original="orig", suggestion_colloquiale="sugg-c",
                              suggestion_soutenue="sugg-s")
    language_coach.update_idiolecte(p, [correction], word_count=10, date="2026-09-19", doc_label="t")
    out = p.read_text(encoding="utf-8")
    assert "Aucune classe pour l'instant" not in out
    assert "### A — Calco" in out


def test_update_idiolecte_no_corrections_leaves_file_untouched(tmp_path):
    path = _write_fixture(tmp_path)
    before = path.read_text(encoding="utf-8")
    dominant = language_coach.update_idiolecte(path, [], word_count=10, date="2026-09-19", doc_label="t")
    assert dominant == ""
    assert path.read_text(encoding="utf-8") == before


def _update_concurrent(path, i):
    for k in range(5):
        n = f"{i}-{k}"
        c = _correction(original=f"faute {n}", suggestion_colloquiale=f"juste {n}",
                        suggestion_soutenue=f"soutenu {n}")
        language_coach.update_idiolecte(path, [c], word_count=10, date="2026-10-01",
                                        doc_label=f"proc{n}")


def test_update_idiolecte_concurrent_writers_lose_nothing(tmp_path):
    # Panneau et imports de Claude écrivent le même profil en même temps : sans
    # verrou, une écriture en écrasait une autre et une ligne restait tronquée.
    import multiprocessing as mp
    path = _write_fixture(tmp_path)
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_update_concurrent, args=(path, i)) for i in range(8)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    out = path.read_text(encoding="utf-8")
    perdues = [f"{i}-{k}" for i in range(8) for k in range(5) if f"| proc{i}-{k} |" not in out]
    assert perdues == [], f"écritures perdues : {perdues}"
    assert out.count("correction automatique (language_coach) : 1 intervention(s)") == 40
    assert "- **Fréquence** : 86 (classe la plus chargee)" in out    # 6 + 40 x 2


# --- CLI end-to-end (stub, no network) -----------------------------------------------

def _rubric(tmp_path):
    r = tmp_path / "rubric.md"
    r.write_text("Coach de langue.", encoding="utf-8")
    return r


def _vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "anglais").mkdir(parents=True)
    (vault / "italien").mkdir(parents=True)
    (vault / "anglais" / "idiolecte.md").write_text(FIXTURE, encoding="utf-8")
    (vault / "italien" / "idiolecte.md").write_text(FIXTURE, encoding="utf-8")
    return vault


def test_main_stub_writes_preview_no_corrections(tmp_path):
    rubric = _rubric(tmp_path)
    text = tmp_path / "text.txt"
    text.write_text("Some english text.", encoding="utf-8")
    out = tmp_path / "out.txt"
    vault = _vault(tmp_path)

    rc = language_coach.main([
        "--stub", "--text-file", str(text), "--rubric", str(rubric),
        "--output", str(out), "--vault-dir", str(vault),
    ])
    assert rc == 0
    preview = out.read_text(encoding="utf-8")
    assert "Langue detectee : en" in preview
    assert "-- Version colloquiale --" in preview
    assert "-- Version soutenue --" in preview
    assert "texte deja natif" in preview


def test_main_missing_idiolecte_profile_fails(tmp_path, monkeypatch):
    rubric = _rubric(tmp_path)
    text = tmp_path / "text.txt"
    text.write_text("Some text.", encoding="utf-8")
    out = tmp_path / "out.txt"
    err = tmp_path / "err.txt"
    vault = tmp_path / "vault"
    vault.mkdir()

    raw = (
        "LANG: en\n===NATIF_COLLOQUIAL===\nBetter text.\n===NATIF_SOUTENU===\nBetter text.\n"
        "===CORRECTIONS===\nB||||orig|colloq|soutenu|\n"
    )
    monkeypatch.setattr(language_coach, "cloud_chunks", lambda *a, **k: iter([raw]))

    rc = language_coach.main([
        "--text-file", str(text), "--rubric", str(rubric),
        "--output", str(out), "--error-file", str(err), "--vault-dir", str(vault),
    ])
    assert rc == 1
    assert "idiolecte introuvable" in err.read_text(encoding="utf-8")


def test_main_empty_text_fails(tmp_path):
    rubric = _rubric(tmp_path)
    text = tmp_path / "text.txt"
    text.write_text("   \n", encoding="utf-8")
    out = tmp_path / "out.txt"
    rc = language_coach.main([
        "--stub", "--text-file", str(text), "--rubric", str(rubric), "--output", str(out),
    ])
    assert rc == 1
    assert not out.exists()


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))


# --- Issue 003 : separateurs doubles et pertes silencieuses -------------------

_ENTETE = (
    "LANG: en\n===NATIF_COLLOQUIAL===\nx\n===NATIF_SOUTENU===\ny\n===CORRECTIONS===\n"
)


def test_separateurs_doubles_classe_neuve():
    """Le modele double parfois les `|` : 12 champs au lieu de 8. La correction
    doit etre recuperee au lieu d'etre perdue (issue 003)."""
    out = language_coach.parse_model_output(
        _ENTETE
        + "F|Preposition de duree|Tendance|Strategie||Since 3 years I work"
          "||I've been working for 3 years||I have been working for three years||\n"
    )
    assert out["dropped"] == []
    assert len(out["corrections"]) == 1
    c = out["corrections"][0]
    assert c["letter"] == "F"
    assert c["label"] == "Preposition de duree"
    assert c["original"] == "Since 3 years I work"
    assert c["suggestion_colloquiale"] == "I've been working for 3 years"
    assert c["suggestion_soutenue"] == "I have been working for three years"
    assert c["idiome"] == ""


def test_separateurs_doubles_classe_existante():
    out = language_coach.parse_model_output(
        _ENTETE + "B||||I am agree||I agree||I agree||\n"
    )
    assert out["dropped"] == []
    c = out["corrections"][0]
    assert (c["letter"], c["label"]) == ("B", "")
    assert c["original"] == "I am agree"


def test_format_canonique_inchange():
    """Une ligne a 8 champs reste lue exactement comme avant."""
    out = language_coach.parse_model_output(
        _ENTETE + "A|Calque|Tend|Strat|orig|coll|sout|idio\n"
    )
    c = out["corrections"][0]
    assert [c["letter"], c["label"], c["original"], c["idiome"]] == \
           ["A", "Calque", "orig", "idio"]
    assert out["dropped"] == []


def test_ligne_irrecuperable_est_signalee():
    """Ce qui ne peut pas etre lu doit apparaitre dans `dropped` ET dans
    l'apercu : une perte silencieuse est pire qu'une erreur visible."""
    out = language_coach.parse_model_output(_ENTETE + "n'importe quoi\n")
    assert out["dropped"] == ["n'importe quoi"]
    assert "non appliquee" in language_coach.format_preview(out)


def test_dry_run_n_ecrit_pas(tmp_path):
    """--dry-run doit produire l'apercu sans toucher au profil.
    Garde-fou apres un incident : une invocation sans --vault-dir avait
    modifie le vrai vault pendant la mise au point du plugin."""
    langues = tmp_path / "anglais"
    langues.mkdir()
    profil = langues / "idiolecte.md"
    contenu = (
        "## Classes d'erreurs actives\n\n"
        "### A — Calque\n"
        "- **Tendance** : t\n- **Stratégie** : s\n"
        "- **Exemples** :\n  - `x` → `y` (colloquial)\n"
        "- **Fréquence** : 1\n\n"
        "## Métrique de progrès global\n\n"
        "| Date | Doc | Mots | Interventions | /100 mots | Classe dominante |\n"
        "|---|---|---|---|---|---|\n\n"
        "## Journal\n"
    )
    profil.write_text(contenu, encoding="utf-8")
    avant = profil.read_bytes()

    language_coach.STUB_RESPONSE = (
        "LANG: en\n===NATIF_COLLOQUIAL===\nc\n===NATIF_SOUTENU===\ns\n"
        "===CORRECTIONS===\nA||||orig|coll|sout|\n"
    )
    texte = tmp_path / "t.txt"; texte.write_text("orig", encoding="utf-8")
    sortie = tmp_path / "preview.txt"
    code = language_coach.main([
        "--stub", "--dry-run", "--text-file", str(texte),
        "--output", str(sortie), "--vault-dir", str(tmp_path),
    ])
    assert code == 0
    assert profil.read_bytes() == avant, "le profil a ete modifie malgre --dry-run"
    assert "DRY-RUN" in sortie.read_text(encoding="utf-8")


def test_contexte_injecte_et_isole():
    """Le contexte de session doit apparaitre dans le prompt, precede d'une
    consigne explicite de ne pas le corriger : sinon le modele analyserait la
    conversation de l'editeur au lieu du texte soumis."""
    sans = language_coach.build_system_prompt("R", "EN", "IT")
    avec = language_coach.build_system_prompt("R", "EN", "IT", "on parle de rclone")
    assert "Contexte de la session" not in sans
    assert "on parle de rclone" in avec
    assert "N'analyse PAS ce contexte" in avec


def test_contexte_vide_ignore():
    assert language_coach.build_system_prompt("R", "EN", "IT", "   ") == \
           language_coach.build_system_prompt("R", "EN", "IT")


# --- niveau de reflexion (--think) ---------------------------------------------------
# Mesure du 2026-09-30 : sans `think`, gpt-oss consacre ~90 % de ses tokens a une
# reflexion jetee (5-7 s) ; `low` ramene l'appel a ~2 s au format intact.

def test_parse_think():
    assert language_coach.parse_think("low") == "low"
    assert language_coach.parse_think("false") is False
    assert language_coach.parse_think("true") is True
    assert language_coach.parse_think("") is None


def test_main_transmet_think_au_modele(tmp_path, monkeypatch):
    rubric = _rubric(tmp_path)
    text = tmp_path / "text.txt"
    text.write_text("Some text.", encoding="utf-8")
    vus = {}

    def faux(model, system, prompt, host, think=None):
        vus.update(model=model, think=think)
        return iter(["LANG: en\n===NATIF_COLLOQUIAL===\nA.\n===NATIF_SOUTENU===\nA.\n"
                     "===CORRECTIONS===\n"])
    monkeypatch.setattr(language_coach, "cloud_chunks", faux)

    rc = language_coach.main([
        "--text-file", str(text), "--rubric", str(rubric), "--output", str(tmp_path / "o"),
        "--vault-dir", str(tmp_path), "--model", "deepseek-v4.1-flash", "--think", "false",
    ])
    assert rc == 0
    assert vus == {"model": "deepseek-v4.1-flash", "think": False}


def test_cloud_chunks_envoie_think(monkeypatch):
    import io, json
    import prompt_rewrite
    envoye = {}

    def faux_urlopen(req, timeout):
        envoye.update(json.loads(req.data))
        return io.BytesIO(b'{"message":{"content":"ok"},"done":true}\n')
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    monkeypatch.setattr(prompt_rewrite.urllib.request, "urlopen", faux_urlopen)

    assert "".join(prompt_rewrite.cloud_chunks("gpt-oss:120b", "s", "p", "https://h", think="low")) == "ok"
    assert envoye["think"] == "low"
    envoye.clear()
    "".join(prompt_rewrite.cloud_chunks("gpt-oss:120b", "s", "p", "https://h"))
    assert "think" not in envoye


def test_openai_chunks_groq_flux_sse(monkeypatch):
    # Groq parle l'API OpenAI : flux SSE « data: {...} », reflexion dans un champ a
    # part (jamais renvoyee), fin sur « data: [DONE] ».
    import io, json
    import prompt_rewrite
    vu = {}

    def faux_urlopen(req, timeout):
        vu.update(url=req.full_url, auth=req.get_header("Authorization"), body=json.loads(req.data))
        return io.BytesIO(
            b'data: {"choices":[{"delta":{"reasoning":"je pense"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"LANG"}}]}\n\n'
            b': keep-alive\n\n'
            b'data: {"choices":[{"delta":{"content":": en"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(prompt_rewrite.urllib.request, "urlopen", faux_urlopen)

    out = "".join(prompt_rewrite.openai_chunks("openai/gpt-oss-120b", "s", "p", "groq", think="low"))
    assert out == "LANG: en"
    assert vu["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert vu["auth"] == "Bearer k"
    assert vu["body"]["stream"] is True
    assert vu["body"]["reasoning_effort"] == "low"
    assert vu["body"]["messages"][0] == {"role": "system", "content": "s"}


def test_openai_chunks_sans_cle(monkeypatch):
    import prompt_rewrite
    import pytest
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(prompt_rewrite, "linux_secret", lambda *a: None)
    with pytest.raises(RuntimeError, match="secret-tool store .*service groq"):
        "".join(prompt_rewrite.openai_chunks("m", "s", "p", "groq"))


def test_main_provider_groq(tmp_path, monkeypatch):
    text = tmp_path / "t.txt"; text.write_text("Hello there friend.", encoding="utf-8")
    rubric = tmp_path / "r.md"; rubric.write_text("rubrique", encoding="utf-8")
    vus = {}

    def faux(model, system, prompt, provider, think=None):
        vus.update(model=model, provider=provider, think=think)
        return iter(["LANG: en\n===NATIF_COLLOQUIAL===\nA.\n===NATIF_SOUTENU===\nA.\n"
                     "===CORRECTIONS===\n"])
    monkeypatch.setattr(language_coach, "openai_chunks", faux)
    monkeypatch.setattr(language_coach, "cloud_chunks",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Ollama appele")))

    rc = language_coach.main([
        "--text-file", str(text), "--rubric", str(rubric), "--output", str(tmp_path / "o"),
        "--vault-dir", str(tmp_path), "--provider", "groq",
        "--model", "openai/gpt-oss-120b", "--think", "low",
    ])
    assert rc == 0
    assert vus == {"model": "openai/gpt-oss-120b", "provider": "groq", "think": "low"}


def test_habillage_hors_format_ignore_sans_perte():
    # deepseek-v4.1-flash entoure sa reponse d'un bloc ``` et ajoute un commentaire
    # final : ce ne sont pas des corrections perdues (resultat 'partial' a tort).
    raw = (
        "```\nLANG: en\n===NATIF_COLLOQUIAL===\nA.\n===NATIF_SOUTENU===\nA.\n"
        "===CORRECTIONS===\nB||||orig|colloq|soutenu|\n```\n"
        "`idiome_frequent` laisse vide : rien de fige ici.\n"
    )
    parsed = language_coach.parse_model_output(raw)
    assert [c["original"] for c in parsed["corrections"]] == ["orig"]
    assert parsed["dropped"] == []


def test_bloc_ouvert_apres_corrections_ne_perd_rien():
    raw = (
        "LANG: en\n===NATIF_COLLOQUIAL===\nA.\n===NATIF_SOUTENU===\nA.\n"
        "===CORRECTIONS===\n```\nB||||orig|colloq|soutenu|\n```\n"
    )
    parsed = language_coach.parse_model_output(raw)
    assert [c["original"] for c in parsed["corrections"]] == ["orig"]
    assert parsed["dropped"] == []


# --- --stream : un objet JSON par morceau de reponse, sur stdout ----------------------

def test_stream_emet_chaque_morceau_en_ndjson(tmp_path, monkeypatch, capsys):
    import json
    rubric = _rubric(tmp_path)
    text = tmp_path / "text.txt"
    text.write_text("Some text.", encoding="utf-8")
    morceaux = ["LANG: en\n===NATIF_COLLOQUIAL===\nA.\n", "===NATIF_SOUTENU===\nA.\n",
                "===CORRECTIONS===\n"]
    monkeypatch.setattr(language_coach, "cloud_chunks", lambda *a, **k: iter(morceaux))

    rc = language_coach.main([
        "--text-file", str(text), "--rubric", str(rubric), "--output", str(tmp_path / "o"),
        "--vault-dir", str(tmp_path), "--stream",
    ])
    assert rc == 0
    lignes = capsys.readouterr().out.splitlines()
    assert [json.loads(l) for l in lignes] == [{"delta": m} for m in morceaux]


def test_sans_stream_rien_sur_stdout(tmp_path, monkeypatch, capsys):
    rubric = _rubric(tmp_path)
    text = tmp_path / "text.txt"
    text.write_text("Some text.", encoding="utf-8")
    monkeypatch.setattr(language_coach, "cloud_chunks", lambda *a, **k: iter(
        ["LANG: en\n===NATIF_COLLOQUIAL===\nA.\n===NATIF_SOUTENU===\nA.\n===CORRECTIONS===\n"]))
    language_coach.main([
        "--text-file", str(text), "--rubric", str(rubric), "--output", str(tmp_path / "o"),
        "--vault-dir", str(tmp_path),
    ])
    assert capsys.readouterr().out == ""


# --- --response-file : analyse ecrite par Claude, sans appel au modele ---------------

_PROFIL_DEUX_CLASSES = (
    "## Classes d'erreurs actives\n\n"
    "### A — Calque\n"
    "- **Tendance** : t\n- **Stratégie** : s\n"
    "- **Exemples** :\n  - `x` → `y` (colloquial)\n"
    "- **Fréquence** : 1\n\n"
    "### B — Faux-amis de registre\n"
    "- **Tendance** : t\n- **Stratégie** : s\n"
    "- **Exemples** :\n  - `x` → `y` (colloquial)\n"
    "- **Fréquence** : 4\n\n"
    "## Métrique de progrès global\n\n"
    "| Date | Doc | Mots | Interventions | /100 mots | Classe dominante |\n"
    "|---|---|---|---|---|---|\n\n"
    "## Journal\n"
)


def _profil(tmp_path):
    (tmp_path / "anglais").mkdir()
    profil = tmp_path / "anglais" / "idiolecte.md"
    profil.write_text(_PROFIL_DEUX_CLASSES, encoding="utf-8")
    return profil


def test_response_file_remplace_le_modele(tmp_path, monkeypatch):
    profil = _profil(tmp_path)

    def interdit(*a, **k):
        raise AssertionError("le modele ne doit pas etre appele")
    monkeypatch.setattr(language_coach, "cloud_chunks", interdit)
    reponse = tmp_path / "r.txt"
    reponse.write_text(
        "LANG: en\n===NATIF_COLLOQUIAL===\nI want to improve my English.\n"
        "===NATIF_SOUTENU===\nI wish to improve my English.\n"
        "===CORRECTIONS===\nA||||endeavour my English|improve my English|improve my English|\n",
        encoding="utf-8")
    texte = tmp_path / "t.txt"; texte.write_text("I want to endeavour my English.", encoding="utf-8")
    sortie = tmp_path / "preview.txt"
    code = language_coach.main([
        "--response-file", str(reponse), "--text-file", str(texte),
        "--output", str(sortie), "--vault-dir", str(tmp_path), "--date", "2026-09-30",
    ])
    assert code == 0
    contenu = profil.read_text(encoding="utf-8")
    assert "`endeavour my English` → `improve my English` (colloquial)" in contenu
    assert "- **Fréquence** : 3" in contenu
    assert "I want to improve my English." in sortie.read_text(encoding="utf-8")


def test_lettre_inconnue_rejoint_la_classe_de_meme_label():
    contenu = _PROFIL_DEUX_CLASSES
    c = {"letter": "?", "label": "faux-amis de registre", "tendance": "t2", "strategie": "s2",
         "original": "o", "suggestion_colloquiale": "c", "suggestion_soutenue": "s", "idiome": ""}
    apres = language_coach.apply_correction(contenu, c)
    assert "- **Fréquence** : 6" in apres
    assert "### C" not in apres


def test_lettre_inconnue_cree_la_prochaine_lettre_libre():
    c = {"letter": "?", "label": "Prépositions de durée", "tendance": "since 3 years",
         "strategie": "for three years", "original": "since 3 years",
         "suggestion_colloquiale": "for three years", "suggestion_soutenue": "for three years",
         "idiome": ""}
    apres = language_coach.apply_correction(_PROFIL_DEUX_CLASSES, c)
    assert "### C — Prépositions de durée" in apres
    assert "### ? " not in apres


def test_parse_accepte_la_lettre_inconnue():
    raw = ("LANG: en\n===NATIF_COLLOQUIAL===\nc\n===NATIF_SOUTENU===\ns\n"
           "===CORRECTIONS===\n?|Label|t|s|orig|coll|sout|\n")
    parsed = language_coach.parse_model_output(raw)
    assert parsed["corrections"][0]["letter"] == "?"
    assert parsed["dropped"] == []


def test_lettre_inconnue_resolue_dans_metrique_et_journal(tmp_path):
    profil = _profil(tmp_path)
    c = {"letter": "?", "label": "Prépositions de durée", "tendance": "t", "strategie": "s",
         "original": "since 3 years", "suggestion_colloquiale": "for three years",
         "suggestion_soutenue": "for three years", "idiome": ""}
    dominante = language_coach.update_idiolecte(profil, [c], 10, "2026-09-30", "Claude Code")
    contenu = profil.read_text(encoding="utf-8")
    assert dominante == "C"
    assert "| 2026-09-30 | Claude Code | ~10 | 1 | 10.0 | C |" in contenu
    assert "classe(s) C." in contenu
