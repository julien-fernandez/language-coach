# Language Coach

A writing coach for English and Italian. It critiques a text with a language model, files each
mistake into a family of recurring errors (word-for-word idioms, false friends, borrowed tenses…)
and tracks how often each family comes back, so practice targets the habits that matter.

**Stage:** working prototype, 40 automated tests. Italian is planned in the code; only the
English error classes exist so far.

## What it does

1. Takes an English or Italian text.
2. Asks a model (Ollama, local or cloud) for a teaching critique in a fixed contract:
   `LANG:` / `===NATIF_COLLOQUIAL===` (natural rewrite) / `===NATIF_SOUTENU===` (formal
   rewrite) / `===CORRECTIONS===` (one line per mistake).
3. Writes the result **directly** into an `idiolecte.md` profile kept in an Obsidian vault:
   each correction is assigned an error class, its frequency is incremented, and a metrics table
   and a log are maintained by the script.

Error classes so far: A word-for-word idiom · B register false friend · C abstract noun used
for a concrete one · D borrowed verb · E borrowed compound tense. A correction may carry the
letter `?`: it joins the class with the same label, or creates the next free letter.

The choice to write without human review is explained in
[`docs/0018-decision-idiolecte.md`](docs/0018-decision-idiolecte.md) (decision record, in French).

## Corrections written by another agent (`--response-file`)

When a coding agent (Claude Code) corrects the user's English during a session, it writes the
answer itself in the same contract. `--response-file` takes it as is: no model call, same profile
update. That is how corrections made in everyday work feed the same profile.

## Try it offline

`--stub` avoids any network call:

```sh
echo "I have 25 years old." > /tmp/t.txt
cd tools && python3 language_coach.py --stub --text-file /tmp/t.txt \
  --output /tmp/preview.txt --vault-dir /tmp/vault
```

## Configuration

- Vault: `--vault-dir`, or `LANGUAGE_COACH_VAULT` (default `~/Obsidian/vault`).
- Model: `language_coach_config.ini` (`model`, `host`).
- API key for Ollama cloud: `OLLAMA_API_KEY` in the environment.

## Tests

```sh
PYTHONPATH="$PWD/tools" python -m pytest tools/tests -q   # 40 passed
```

Standard library only at runtime.

## License

MIT, see [`LICENSE`](LICENSE).
