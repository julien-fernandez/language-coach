---
numero: 0018
titre: Le coach langue écrit le profil idiolecte sans relecture humaine
statut: accepté
date: 2026-09-19
concerne:
  - modules/ui/language_coach_window.ahk
  - tools/language_coach.py
---

# 0018 — Le coach langue écrit le profil idiolecte sans relecture humaine

## Contexte

`Noyau/Coaches/Coach Tekhnè/langues/anglais/idiolecte.md` (vault Obsidian) est jusqu'ici un
document **curé à la main** : chaque classe d'erreur (A calque d'idiome, B faux-ami de registre,
C nom abstrait matériel, D verbe calqué, E temps composé calqué) porte une fréquence, des
exemplars choisis, un tableau de métrique *interventions/100 mots* et un journal narratif. Ces
quatre éléments sont le résultat d'un jugement — décider qu'une erreur relève d'une classe
existante ou en ouvre une nouvelle, décider quand une fréquence mérite d'être notée « à
surveiller » — que seul Julien a fait jusqu'à présent, en session `madrelingua compose`.

Le nouveau module `language_coach_window.ahk` (fenêtre flottante globale, hors session Claude,
sur le modèle de `prompt_rewrite_window.ahk`) capture n'importe quel texte anglais ou italien
tapé dans n'importe quelle application, et produit une critique pédagogique via un modèle Ollama
cloud. La question posée : cette critique met-elle à jour le profil idiolecte **directement et
intégralement** (classe assignée, fréquence incrémentée, tableau de métrique et journal tenus par
le script), ou seulement en **dépôt brut** (une section « captures automatiques » non triée, à
reclasser plus tard par Julien) ?

## Décision

Le script `tools/language_coach.py` écrit **directement et intégralement** dans
`langues/{anglais,italien}/idiolecte.md` : il choisit la classe d'erreur (ou en propose une
nouvelle), incrémente la fréquence, met à jour le tableau de métrique et ajoute une entrée de
journal — sans étape de relecture humaine avant écriture. `langues/italien/idiolecte.md` est créé
dès la mise en place du module, au même gabarit que le profil anglais, plutôt qu'à la demande au
premier run.

## Conséquences

- Une correction fondée sur une mauvaise détection de classe pollue directement le fichier de
  référence, sans étape de rattrapage avant que ça n'arrive — la seule vérification possible est
  a posteriori, à la relecture du fichier par Julien.
- La fréquence des classes (`Fréquence : N`) mesure désormais **des jugements du modèle**, pas
  uniquement des jugements de Julien, avec un risque de dérive si le modèle classe différemment
  d'une session à l'autre (pas de garde de cohérence entre deux runs sur le même type d'erreur).
- Le journal, jusqu'ici un récit rédigé après coup, reçoit des entrées générées automatiquement à
  chaque correction — sa nature narrative en est diluée.
- Historique git du fichier : chaque correction produit un commit potentiel (ou une modification
  non commitée) sur un fichier vécu comme un journal de progrès personnel — pas de garde-fou
  proposé ici contre l'accumulation de bruit.
- Revenir à un dépôt brut (option écartée ci-dessous) après coup demande de trier a posteriori
  les entrées déjà mêlées à la curation manuelle antérieure — pas un simple flag à inverser.

## Alternatives écartées

- **Dépôt brut en section « captures automatiques »** (option recommandée à l'origine) :
  écarté par Julien au profit de l'automatisation complète — le gain de vitesse (le profil
  s'enrichit sans geste de tri différé) l'emporte sur le risque de pollution, assumé
  explicitement.
- **Création paresseuse du profil italien au premier run** : écartée au profit d'une création
  immédiate, pour éviter un premier run qui échoue ou improvise un gabarit au moment de la
  correction.
