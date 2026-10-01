<!--
Merci. Quelques rappels avant d'ouvrir la PR — ils viennent de CONTRIBUTING.md.

- Un modèle de chat en service, c'est de la production : ne le redémarre pas pour
  tester ta modification. Si ta PR touche `vllm-runner/runner.py`, `systemd/` ou le
  catalogue de modèles, dis-le explicitement plus bas.
- Déployer = reconstruire l'image puis `docker compose up -d <service>`.
  `docker compose restart` ne prend PAS le nouveau code.
- Jamais de `--memory` sur un sidecar (la mémoire est unifiée, le plafond casse
  aussi le chargement CUDA).
- Aucun secret, aucune clé, aucune donnée personnelle dans la PR ni dans les
  captures d'écran (le dépôt est public).
-->

## Ce que fait cette PR

<!-- En une ou deux phrases : le problème réel, pour qui il se posait, et ce que
     la modification change. -->

## Pourquoi ainsi

<!-- Les options envisagées, celle retenue, et ce qui ferait revenir dessus.
     S'il s'agit d'un correctif non évident, colle la MESURE qui le prouve
     (commande + sortie) : plusieurs commentaires du dépôt n'existent que pour
     consigner une mesure qui contredit le correctif intuitif. -->

## Vérifications

- [ ] `./scripts/pre-push-check.sh` est vert (tests + i18n + scan de secrets)
- [ ] Si le frontend change : `npx tsc --noEmit` et `npx eslint .` sont verts
- [ ] Si une page change d'apparence : la galerie de `assets/` a été refaite
      (`scripts/screenshots.py` puis `--verify` puis `--install`)
- [ ] Si le contrat HTTP, `.env` ou le déploiement change : `CHANGELOG.md` a une
      entrée sous `## [Unreleased]`
- [ ] Aucun secret, aucune clé API, aucune donnée personnelle ajoutés
- [ ] Le modèle servi n'a **pas** été redémarré

## Impact opérationnel

<!-- À remplir si applicable : le changement ne prend effet qu'au prochain
     lancement légitime du runner (le dire explicitement !), une unité systemd
     doit être réinstallée, une variable `.env` doit être ajoutée à la main,
     une migration de base est nécessaire, une image doit être reconstruite. -->

Aucun.
