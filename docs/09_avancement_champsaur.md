# 09 — Avancement partagé : Champsaur

Journal opérationnel pour les allers-retours entre Codex, Claude et le développeur.
But: garder une trace courte de ce qui a été tenté, de ce qui marche, de ce qui reste à
faire, et des hypothèses prises sur la vallée du Champsaur.

## Zone d'étude initiale

- Nom court: `champsaur`
- Région: vallée du Champsaur, Hautes-Alpes, France.
- Emprise initiale WGS84: ouest `5.95`, sud `44.55`, est `6.42`, nord `44.86`.
- Taille approximative: ~37 km est-ouest x ~34 km nord-sud, donc sous la limite pratique
  WindNinja de ~50 km par côté.
- Centres/repères inclus approximativement: Saint-Bonnet-en-Champsaur, Ancelle,
  Pont-du-Fossé, Orcières, Saint-Michel-de-Chaillol, Champoléon.
- DEM initial: Open-Meteo Elevation API, grille grossière par défaut `2000 m`, pour lancer
  rapidement la chaîne. À remplacer ensuite par IGN RGE ALTI 5 m / 1 m.

## Règle de sécurité à conserver

La carte Pass 1 affiche un indicateur de probabilité / sévérité de zone sous le vent.
Elle ne montre pas les rotors. Les rotors nécessitent le Pass 2 momentum / OpenFOAM sur
une zone locale.

## Journal

### 2026-06-21 — Initialisation Champsaur avec Codex

Actions:
- Ajout d'un domaine réutilisable `CHAMPSAUR` dans `src/sillage/areas.py`.
- Ajout d'un export GeoTIFF UTM pour les DEM préparés dans `terrain/dem.py`.
- Ajout de `scripts/champsaur_map.py` pour:
  - créer/télécharger un DEM initial via Open-Meteo Elevation,
  - reprojeter et exporter le DEM préparé UTM,
  - calculer l'indicateur Pass 1 en mode geometry-only,
  - enregistrer une carte PNG et lister les meilleurs candidats.

Hypothèses:
- Vent de départ par défaut: `320°` météo, `8 m/s`, représentatif d'un premier cas de
  travail NW. À ajuster selon le jour de vol ou le scénario.
- Le DEM Open-Meteo est seulement un bootstrap. Il sert à vérifier la chaîne et la forme
  générale du relief, pas à prendre une décision de vol.

À faire ensuite:
- Remplacer le DEM bootstrap par un vrai extrait IGN RGE ALTI.
- Fiabiliser la lecture des sorties ASCII WindNinja u/v pour ajouter le terme de déficit
  de vitesse.
- Ajouter le slider horaire et le clic hotspot pour préparer le Pass 2.
- Choisir un premier spot/arête test pour un run momentum local.

### 2026-06-21 — Note API Open-Meteo Elevation

Observation:
- Une première tentative en grille `600 m` a reçu un HTTP 429 après environ 600 points.

Décision:
- Le script utilise maintenant une grille bootstrap par défaut `2000 m` et ajoute
  `--request-delay` avec retry sur 429. La grille `600 m` reste possible, mais il faudra
  la lancer plus lentement ou passer rapidement à un DEM local IGN.

### 2026-06-21 — Robustesse réseau bootstrap DEM

Observation:
- Une relance plus grossière `2000 m` a passé 200/360 points puis a rencontré une
  fermeture de connexion distante (`ConnectionResetError`).

Décision:
- Le script retente maintenant aussi les erreurs réseau transitoires, pas seulement les
  HTTP 429. Pour Open-Meteo, lancer avec un délai de quelques secondes si nécessaire.

### 2026-06-21 — Première carte Champsaur générée

Commande utilisée:
```bash
python scripts/champsaur_map.py --download-dem --grid-spacing 2000 --request-delay 5 --wind-dir 320 --wind-speed 8 --save outputs/champsaur/champsaur_pass1_geometry.png
```

Résultat:
- DEM source bootstrap: `cache/champsaur/champsaur_open_meteo_wgs84.tif`.
- DEM préparé UTM WindNinja: `cache/champsaur/champsaur_prepared_utm.tif`.
- Carte PNG: `outputs/champsaur/champsaur_pass1_geometry.png`.
- DEM préparé: `19 x 20` pixels, résolution ~`1886 m`, domaine ~`37.7 x 35.8 km`, CRS `EPSG:32632`.
- Carte vérifiée techniquement: image PNG `975 x 1139`, non vide.

Limite:
- Résolution trop grossière pour une analyse réelle. Cette carte valide seulement le fil
  terrain -> reprojection -> indicateur geometry-only -> rendu. Prochaine étape sérieuse:
  importer un DEM IGN RGE ALTI et descendre la résolution terrain/Pass 1.


### 2026-06-21 — DEM IGN RGE ALTI 5 m Champsaur

Source officielle:
- Catalogue CSW Géoplateforme: `https://data.geopf.fr/catalog?service=CSW&version=2.0.2&request=GetCapabilities`.
- Ressource RGE ALTI: `https://data.geopf.fr/telechargement/resource/RGEALTI`.
- Archive utilisée: `RGEALTI_2-0_5M_ASC_LAMB93-IGN69_D005_2020-10-14`.
- URL archive: `https://data.geopf.fr/telechargement/download/RGEALTI/RGEALTI_2-0_5M_ASC_LAMB93-IGN69_D005_2020-10-14/RGEALTI_2-0_5M_ASC_LAMB93-IGN69_D005_2020-10-14.7z`.
- Taille archive: `468317386` octets.

Actions:
- Ajout de la dépendance `py7zr` pour lire/extraire les archives `.7z` IGN.
- Ajout de `scripts/prepare_champsaur_ign.py`.
- Téléchargement de l'archive D005 5 m dans `cache/champsaur/ign/`.
- Sélection de `72` dalles ASC intersectant l'emprise Champsaur.
- Extraction ciblée dans `cache/champsaur/ign/extracted_champsaur_5m`.
- Mosaïque/crop Lambert-93 5 m: `cache/champsaur/ign/champsaur_rgealti_5m_l93.tif`.
- DEM d'analyse UTM 50 m: `cache/champsaur/ign/champsaur_rgealti_50m_utm.tif`.
- DEM préparé Sillage/WindNinja UTM 50 m: `cache/champsaur/ign/champsaur_rgealti_50m_prepared_utm.tif`.
- Carte geometry-only IGN: `outputs/champsaur/champsaur_pass1_geometry_ign50m.png`.

Commande utilisée:
```bash
python scripts/prepare_champsaur_ign.py --analysis-resolution 50 --wind-dir 320 --wind-speed 8 --save outputs/champsaur/champsaur_pass1_geometry_ign50m.png
```

Résultats techniques:
- Crop 5 m Lambert-93: `7718 x 7181`, altitude min/max/moyenne ~`707 / 3436 / 1686 m`.
- DEM 50 m UTM: `824 x 775`, domaine ~`41.2 x 38.8 km`, CRS `EPSG:32632`.
- Carte PNG IGN: `968 x 1139`, non vide, ~`1.0 Mo`.

Limites:
- Carte encore `geometry-only`: pas de WindNinja mass, donc pas encore de déficit de vitesse.
- Les scores candidats plafonnent autour de `0.50`; à revoir quand l'indicateur intègre
  les champs de vent et quand on affine la pondération terrain/shelter.

### 2026-06-21 — Pass 1 WindNinja mass Champsaur

Observation bord de carte:
- La carte `geometry-only` montrait des artefacts en bordure. C'est attendu avec les
  filtres morphologiques et l'indice Winstral sur un DEM croppe: pres des limites, le
  calcul n'a pas le relief hors emprise.
- `scripts/champsaur_pass1_mass.py` masque maintenant une marge de bord (`--edge-buffer`,
  defaut `1500 m`) avant de classer les candidats.

Corrections WindNinja:
- Le binaire WindNinja 3.12 demande explicitement `--vegetation`; le wrapper utilise
  maintenant `vegetation="grass"` par defaut, modifiable.
- Le binaire demande aussi `mesh_resolution` ou `mesh_choice`; pour le Pass 1 mass, le
  wrapper fixe `mesh_resolution = output_resolution_m`.
- Point critique: `output_speed_units` vaut `mph` par defaut dans WindNinja. Le wrapper
  force maintenant `--output_speed_units=mps`. Avant correction, la grille vitesse avait
  une moyenne ~`16.57` et un max ~`63.79`, en fait des mph. Apres correction: moyenne
  ~`7.4 m/s` pour un vent injecte `8 m/s`.

Runs effectues:
```bash
python scripts/champsaur_pass1_mass.py --wind-dir 320 --wind-speed 8 --resolution 100 --edge-buffer 1500 --force-run --save outputs/champsaur/champsaur_pass1_mass_320_8_100m.png
python scripts/champsaur_pass1_mass.py --wind-dir 320 --wind-speed 8 --resolution 50 --edge-buffer 1500 --force-run --save outputs/champsaur/champsaur_pass1_mass_320_8_50m.png
```

Resultats:
- Sorties WindNinja 100 m: `cache/champsaur/windninja_mass_320_8_100m/*_{ang,cld,u,v,vel}.asc`.
- Sorties WindNinja 50 m: `cache/champsaur/windninja_mass_320_8_50m/*_{ang,cld,u,v,vel}.asc`.
- Carte Pass 1 mass 100 m: `outputs/champsaur/champsaur_pass1_mass_320_8_100m.png`.
- Carte Pass 1 mass 50 m: `outputs/champsaur/champsaur_pass1_mass_320_8_50m.png`.
- Run 50 m: grille vitesse `775 x 824`, min/moy/max ~`0.02 / 7.38 / 36.37 m/s`.
- Top candidat 50 m: environ `(x=269829, y=4958344)`, score `0.84`.

Limites:
- Toujours Pass 1: candidats de zone sous le vent, pas rotors.
- Vent encore domain-average fixe (`320 deg`, `8 m/s`), pas encore profil horaire meteo.
- Le terme deficit vitesse est maintenant actif, mais l'indicateur reste empirique et
  devra etre calibre/compare avec des cas connus.

Verification finale de cette passe:
- Nettoyage de `docs/05_windninja_integration.md`: suppression des sequences litterales `` `r`n`` ajoutees par erreur lors d'une insertion PowerShell.
- Tests relances: `python -m pytest -q` -> `12 passed`, avec seulement l'avertissement non bloquant connu sur `.pytest_cache` (`WinError 183`).

### 2026-06-21 — Migration des artefacts generes vers C:\A2K\SousLeVent (Claude)

Contexte:
- Suite Codex: `config.py` centralise desormais les sorties hors arbre source. Restait a
  finir la migration physique, corriger `pyproject.toml`, et consigner l'etape.

Actions:
- `config.py` (deja pose par Codex, verifie): `generated_root` par defaut
  `C:\A2K\SousLeVent` sous Windows; `cache_dir`/`output_dir`/`temp_dir` derives;
  `resolve_cache_path`/`resolve_output_path`/`resolve_temp_path` acceptent les anciens
  prefixes `cache/`, `outputs/`, `tmp/`; `TMP`/`TEMP`/`TMPDIR` epingles sur `<root>\tmp`.
- `.env` et `.env.example`: introduction de `SILLAGE_GENERATED_ROOT=C:\A2K\SousLeVent`,
  suppression de l'override `SILLAGE_CACHE_DIR=./cache` qui aurait annule la migration.
- Migration physique (meme volume C:, renommage instantane, aucune copie):
  - `cache/` (~1.15 Go, inclut l'archive IGN 5 m et les DEM prepares) -> `C:\A2K\SousLeVent\cache`.
  - `outputs/` (4 PNG Champsaur) -> `C:\A2K\SousLeVent\outputs`.
  - L'arbre source ne contient plus `cache/` ni `outputs/`.
- `pyproject.toml`: correction de la ligne cassee `` `r`n ``. La sequence avait avale
  `py7zr` DANS le commentaire de `python-dotenv`, donc `py7zr` n'etait pas declare comme
  dependance (il n'etait installe qu'a la main). Desormais `py7zr>=1.1` est bien declare
  (deja installe en 1.1.3).
- Garde `dry_run` (deja posee par Codex, verifiee): `run_mass`/`run_momentum` ne creent
  plus le `working_dir` en `dry_run=True`. C'etait la cause des dossiers fantomes.
- Nettoyage fantomes: suppression de `C:\tmp\wnd_mass` et `C:\tmp\wnd_mom` (crees par les
  anciens tests `dry_run` avant la garde mkdir). `C:\tmp` lui-meme est protege par le
  sandbox et laisse en place (vide). `C:\tmp_scottplot` / `C:\tmp_scottplot2` ne sont PAS
  lies a SousLeVent (mini-projet C# ScottPlot du 19/05): laisses intacts.

Verifications:
- `load_config()` -> `generated_root=C:\A2K\SousLeVent`, cache/outputs/tmp derives dessous,
  `TMP=C:\A2K\SousLeVent\tmp`.
- `resolve_output_path('outputs/champsaur/x.png')` -> `C:\A2K\SousLeVent\outputs\champsaur\x.png`.
- DEM cle present apres migration: `cache\champsaur\ign\champsaur_rgealti_50m_prepared_utm.tif`.
- `python -m pytest -q` -> `12 passed`; `C:\tmp\wnd_*` non recrees; arbre projet propre.

Limites / a noter:
- `.env` reste non versionne (gitignore). Sur une autre machine, definir
  `SILLAGE_GENERATED_ROOT` (ou laisser le defaut) avant de relancer les scripts.

### 2026-06-21 — M1 boucle horaire + slider, et de-risque Pass 2 (Claude)

Deux livrables demandes: finir M1 (boucle horaire + slider) ET trancher si le momentum
tourne nativement sous Windows.

M1 — boucle horaire Pass 1 + carte time-sliderable:
- `terrain/dem.py`: ajout de `crop_dem(...)` (fenetre rectangulaire centree, clippee a
  l'emprise) — sert au crop Pass 2.
- `viz/map2d.py`: `show_timeline(...)` implemente (slider matplotlib, ex-`NotImplementedError`)
  + `save_timeline_gif(...)` (GIF anime headless via PillowWriter, sans affichage).
- `screening/pass1.py` (nouveau): `hourly_indicator(...)` = run_mass (ou reuse cache) ->
  grille vitesse -> indicateur -> masque de bord, pour UNE heure.
- `scripts/champsaur_pass1_hourly.py` (nouveau): boucle sur les heures (`--source forecast`
  Open-Meteo, ou `--source synthetic` deterministe offline), cache par heure sous
  `<cache>/champsaur/hourly/h{NN}_...`, GIF + slider (`--show`).
- Run de validation: `--source synthetic --hours 4 --resolution 100` -> 4 runs mass,
  candidats top variant avec le vent (scores ~0.85-0.87), GIF `outputs/champsaur/
  champsaur_pass1_hourly.gif` (~1 Mo, 4 frames). M1 essentiellement boucle.

Pass 2 — VERDICT: le solveur momentum tourne NATIVEMENT sous Windows (pas besoin de Docker):
- `scripts/pass2_smoke_test.py` (nouveau): crop ~5x5 km autour du top candidat
  `(269829, 4958344)`, run momentum minimal, verdict OK / NO-FOAM / OTHER.
- Flag manquant trouve: WindNinja 3.12 exige `write_goog_output` quand
  `turbulence_output_flag=true` (`Exception caught: Option 'turbulence_output_flag'
  requires option 'write_goog_output'`). Corrige dans `flow/windninja.run_momentum`
  (ajoute `--write_goog_output=true` si turbulence active).
- Apres correction: `rc=0`, solver complet (`Solving for the flow field... Run number 0
  done!`), ~118 s pour mesh=25000 / iters=100 (crop 101x101 a 50 m).
- Emplacement du case OpenFOAM: NinjaFOAM ecrit `NINJAFOAM_<dem>_<pid>_<n>` dans le
  dossier du DEM d'entree (pas dans le working_dir, qui ne recoit que le kmz + sorties
  echantillonnees). `locate_openfoam_case` corrige: cherche les dossiers `NINJAFOAM_*` et
  scanne aussi `extra_roots` (passe le dossier du DEM); prend le plus recent.
- Chaine de lecture 3D validee sur le case reel (`flow/openfoam_reader`): 65697 cellules,
  champs `p, U, epsilon, k, nut`; ~16830/65697 cellules a flux inverse (~26 %, la
  recirculation sous le vent); intensite de turbulence moyenne ~0.17, max ~0.53.

Tests: `python -m pytest -q` -> `18 passed` (12 -> 18; ajout crop_dem, locate_openfoam_case,
flag goog momentum, GIF timeline).

Implication archi: la question ouverte "Pass 2 sous Windows: a verifier" est tranchee
POSITIVEMENT pour ce build (WindNinja 3.12, OpenMP 20). Docker n'est PAS requis pour le
solve. Reste pour finir M2: le rendu 3D `viz/volume3d` (streamlines + volumes flux
inverse / turbulence) et le script `demo_pass2_single` de bout en bout.

A faire ensuite:
- Tester/regler `viz/volume3d.show(...)` sur le case reel (rendu PyVista).
- M3: handoff clic-hotspot Pass 1 -> Pass 2 (lire le vent crete amont au candidat,
  cropper+buffer, lancer le momentum, afficher la scene 3D).
- Brancher `--source forecast` sur une vraie fenetre horaire (et cacher le JSON Open-Meteo).

### 2026-06-21 — Rendu 3D Pass 2 (volume3d) — M2 boucle (Claude)

Objectif: finir le dernier morceau de M2, le rendu 3D de la recirculation.

Actions:
- `flow/openfoam_reader.py`: ajout de `read_terrain_stl(case)` — lit la surface terrain
  que NinjaFOAM derive du DEM (`constant/triSurface/<dem>.stl`, hors `_out.stl`).
- `viz/volume3d.py` reecrit:
  - terrain (STL, colore par elevation, cmap gist_earth),
  - volume de FLUX INVERSE (along-flow < 0) en orange = le rotor,
  - volume d'INTENSITE DE TURBULENCE (option, seuil par defaut 0.2),
  - streamlines amont (best-effort, cell->point U, tube),
  - `save_png(...)` rendu HEADLESS off-screen (PNG) + `show(...)` interactif,
  - `mean_flow_vector(wind_from_deg)` (vecteur unite "vers ou souffle le vent").
- `scripts/demo_pass2_single.py`: branche bout-en-bout, options `--save` (PNG headless),
  `--turbulence`, `--no-show`; utilise `volume3d.mean_flow_vector`.

Verifications:
- Rendu headless du case reel -> `outputs/champsaur/pass2_smoke_3d_default.png` (250 Ko):
  terrain + volume rotor orange bien visible dans le creux sous le vent. PyVista off-screen
  fonctionne sous Windows (VTK 9.6), pas de display requis.
- Avec turbulence seuil 0.2: le volume couvre presque tout le domaine (TI moyenne ~0.17),
  donc turbulence OFF par defaut; utiliser un seuil plus haut pour isoler les pires zones.
- Tests: `python -m pytest -q` -> `20 passed` (+ mean_flow_vector, read_terrain_stl).
- `demo_pass2_single.py --help` OK; py_compile de tous les nouveaux fichiers OK.

Etat M2: la chaine Pass 2 est complete de bout en bout sous Windows natif
(crop -> momentum -> case OpenFOAM -> lecture 3D -> volumes/rendu). Reste du polish
(streamlines plus lisibles, surface terrain opaque vs volumes) et le handoff M3.

### 2026-06-25 — Revue corrective WindNinja / error boxes (Codex)

Contexte:
- Demande: revue complete puis correction des bugs recurrents WindNinja signales dans l'IHM
  ("Erreur WindNinja").
- Tests hors sandbox au depart: `57 passed`, mais revue manuelle/probes sur le binaire local
  ont revele un bug non couvert.

Corrections:
- `flow/windninja.py`: `FLAG["num_threads"]` passe de `number_of_threads` a `num_threads`.
  Verification binaire: WindNinja 3.12 expose `--num_threads`; `--number_of_threads` retourne
  `Exception caught: unknown option number_of_threads`. Cause directe probable des erreurs
  d'affinage spatial/sous-zones.
- `config.py` / `flow/windninja.py`: `load_config()` ne modifie plus globalement
  `TMP`/`TEMP`/`TMPDIR`. `_subprocess_env(tmp_dir=None)` restaure les variables temp systeme
  capturees avant config, pour que momentum/OpenFOAM ne herite plus du temp projet. Les runs
  mass concurrents gardent leur temp isole explicite (`<tile workdir>/_wn_tmp`) + cache PROJ
  isole.
- `screening/pass1.py`: recherche `*_vel.asc` / `*_ang.asc` par fichier le plus recent;
  `hourly_indicator` passe un temp explicite sous le workdir mass.
- IHM `main_window.py`: dossiers de travail WindNinja incluent le stem du DEM actif
  (evite de relire des sorties d'une autre AOI); Pass-2 utilise `format_run_failure`, donc
  les boites d'erreur contiennent maintenant rc, cwd, commande, stderr et stdout.
- `terrain/acquire.py`: le fallback IGN -> Monde ne masque plus les annulations ni les erreurs
  quand l'utilisateur a demande explicitement `IGN`.
- Scripts CLI: memes diagnostics d'erreur WindNinja et temp mass explicite sur les demos.

Docs/tests:
- `docs/03_decisions.md`, `docs/support/environment.md`, `docs/support/troubleshooting.md`
  et `docs/06_dev_log.md` alignes sur `--num_threads` et la regle temp.
- Tests ajoutes/ajustes: verification `--num_threads`, non-fuite du TMP projet dans
  `_subprocess_env()` sans `tmp_dir`, PROJ cache isole avec `tmp_dir`.

Verification avant commit:
- `.\.venv\Scripts\python.exe -m pytest -q` -> `58 passed`
- Probe binaire: `WindNinja_cli --help` expose `--num_threads` et pas
  `--number_of_threads`.

### 2026-06-25 — Acceleration calculs: heures Pass-1 paralleles + timings (Codex)

Idees consignees:
- Parallele le moins risque: les **heures Pass-1** sont independantes. On peut lancer plusieurs
  WindNinja mass en meme temps, avec un cap workers/threads pour ne pas saturer la machine.
- Deja realise avant cette passe: les **sous-zones spatial refine** tournent en parallele
  (`ThreadPoolExecutor`) avec temp/cache PROJ isoles et retry sequentiel en cas d'echec.
- A ne pas faire tout de suite: decouper un **unique Pass-2 momentum** en tuiles. Trop risque
  cote conditions limites; on garde le domaine bufferise + clip visuel.
- Prochaines idees utiles: cache `.npz` des stacks horaires, cache meteo/GRIB, benchmark
  `--workers 1/2/4`, et plus tard parallele sur plusieurs Pass-2 independants.

Realise:
- `src/sillage/timing.py`: `RunTimings` pour consigner des durees de phases.
- `screening/pass1.py`: `hourly_indicator_stack(...)` calcule plusieurs heures en parallele,
  preserve l'ordre du creneau et transmet `--num_threads` a chaque run WindNinja.
- IHM `on_run_hourly`: le criblage temporel utilise maintenant la pile parallele
  (auto: max 4 heures concurrentes, max 4 threads/run) et affiche un resume de timings.
- `scripts/champsaur_pass1_hourly.py`: meme chemin parallele + option `--workers`.
- Tests ajoutes: concurrence reelle via `threading.Barrier`, ordre conserve, cap threads,
  resume de timings.

Verification:
- Tests cibles `tests/test_screening.py` -> `11 passed`.
- Import `python -B ...` -> OK.
- Suite complete `.\.venv\Scripts\python.exe -m pytest -q` -> `61 passed`.

### 2026-06-26 — Revue/consolidation mode auto avant gros tests (Codex)

Contexte:
- Demande: revue des nouveautes faites avec Claude en mode auto, consolidation avant gros
  tests terrain/WindNinja.

Risques corriges:
- Cache Pass-1 auto trop large: `run_auto` reutilisait un dossier `auto/screening` fixe.
  Le cache est maintenant cle par DEM + vent representatif + resolution pour eviter qu'une
  nouvelle route relise une ancienne grille vitesse.
- Parallele momentum par defaut trop agressif: retour a un defaut prudent
  `min(4, coeurs detectes)`, avec slider UI jusqu'a tous les coeurs pour benchmarks.
  Note 2026-07-01: ce point est historique et supersede; le defaut courant redemande tous les
  coeurs detectes, avec plafonnement par le nombre de taches et plan CPU explicite.
- Compactage disque: les cases sans rotor clippe sont maintenant supprimees comme les autres
  (pas de conservation d'un OpenFOAM complet pour afficher "rien").
- Artefact de bord: le clip 3D manuel garde son comportement protecteur, mais le mode auto
  peut maintenant obtenir un rotor vide quand tout le volume etait hors domaine utile.
- Stop disque: l'alerte espace disque se propage aussi aux solves momentum deja en cours.
- Vent AROME route: les fleches sont videes au changement de route et les resultats d'une
  ancienne route sont ignores.
- Docs: `ADR-0017b` pour le parallele horaire Pass-1; `ADR-0022` conserve le mode auto.

Verification:
- Tests cibles auto/pass2: `48 passed`.
- Suite complete: `.\.venv\Scripts\python.exe -m pytest -q` -> `82 passed`.

### 2026-06-26 — Info CPU/parallele dans la selection auto (Codex)

Demande:
- Afficher pendant la selection trajet/creneau combien de calculs peuvent tourner en
  parallele et comment les coeurs sont repartis par division entiere.

Realise:
- Ajout de `momentum_parallel_plan(...)`: calcule `workers`, `threads/worker`,
  coeurs utilises, coeurs au repos et les valeurs qui divisent parfaitement le CPU.
- La fenetre auto affiche maintenant une ligne **Plan CPU** sous le slider:
  `N calculs en parallele x T threads = U/C coeurs`, plus les divisions parfaites
  (ex: `1, 2, 7, 14`) et le plafond utile estime depuis le creneau.
- `run_auto` utilise le meme helper pour que le message de lancement corresponde a l'IHM.

Verification:
- Import sans ecriture `.pyc` OK.
- Tests cibles auto/pass2: `49 passed`.
- Suite complete: `.\.venv\Scripts\python.exe -m pytest -q` -> `83 passed`.

### 2026-06-26 — Correction strategie disque auto: nettoyage fermeture (Codex)

Clarification utilisateur:
- Le probleme disque vient surtout de l'accumulation des resultats apres fermeture du
  programme, pas d'un manque de place pendant le run. Donc le compactage agressif en cours
  de calcul n'est pas le bon defaut s'il peut fragiliser le calcul ou le rendu.

Realise:
- `run_auto` garde maintenant les cases OpenFOAM completes pendant une session IHM normale.
- `_compact_case` reste disponible uniquement en mode optionnel
  `compact_cases_during_run=True`, pas active par defaut.
- Ajout de `cleanup_auto_artifacts(cache_dir)`: supprime les artefacts temporaires auto
  (`NINJAFOAM_*`, `z*_run`, `z*.tif`, `z*.vtu`) sous `<cache>/auto`, en gardant les DEM
  reutilisables et le cache Pass-1 `screening/`. Le glob `z*.vtu` couvre aussi les volumes
  par metrique et les sources re-analysables `*_source.vtu`.
- `AutoWindow.closeEvent`: nettoyage a la fermeture; si un calcul est en cours, demande
  d'abord l'annulation et ne supprime pas sous les pieds d'OpenFOAM.
- Le meme nettoyage reste appele au demarrage du prochain run auto pour reparer les
  fermetures forcees/crashs.

Verification:
- Tests cibles auto/pass2: `48 passed`.
- Suite complete: `.\.venv\Scripts\python.exe -m pytest -q` -> `82 passed`.

### 2026-06-26 — Alignement fond de carte 3D + lecture Pass1/Pass2 (Codex)

Constat utilisateur:
- Le fond de carte 3D est visiblement decale vers le sud de quelques centaines de metres.
- Les resultats sous le vent ne sont pas forcement faux: le vent Pass2 peut etre tres different
  du vent Pass1 utilise pour le criblage, ce qui change fortement l'orientation du rotor.

Diagnostic:
- Bug probable cote rendu 3D: les tuiles de fond arrivent en WebMercator, puis etaient plaquees
  directement sur le terrain UTM. En 2D contextily reprojette, mais en 3D c'est a nous de le faire.
- Petit ecart additionnel possible: le relief 3D etait place sur les bords externes du raster,
  alors que les valeurs MNT sont des echantillons au centre des pixels.
- Cote calcul: `run_auto` utilise un vent representatif pour detecter les features en Pass1, puis
  un vent local par feature et par heure pour les solves Pass2; une divergence visuelle peut donc
  venir de la meteo et pas du georeferencement.

Realise:
- `_drape_basemap` reprojette maintenant le raster WebMercator en CRS terrain avant texture PyVista.
- `_terrain_mesh` place les points du relief aux centres de pixels du MNT.
- Ajout d'une echelle horizontale dans les scenes 3D Pass1, Pass2 manuel et Pass2 auto.
- Le log auto affiche le vent representatif du criblage Pass1 pour comparaison avec les vents Pass2.
- ADR-0027 ajoute la regle de georeferencement 3D.

Verification:
- Tests cibles `tests/test_pass2.py` -> `33 passed`.
- Suite complete `.\.venv\Scripts\python.exe -m pytest -q` -> `86 passed`.
- `git diff --check` OK (warnings LF/CRLF Windows seulement).

### 2026-06-30 — `.sillage` re-analysable: seuils volume modifiables apres ouverture (Codex)

Contexte:
- Retour Claude: les volumes sauvegardes dans un `.sillage` sont figes au seuil par defaut de
  chaque metrique. Changer "Seuil volume" apres reouverture ne peut pas re-extraire sans le case
  OpenFOAM.

Realise:
- Ajout d'un format optionnel `.sillage` v2 **re-analysable**: un `source_XXX.vtu` par cas contient
  la geometrie clippee utile + les scalaires derives (`along_flow`, `along_pct`, `w_ms`, `w_abs`,
  `turb_rms`), sans garder tout OpenFOAM.
- Le rendu auto prefere `source_path` quand present et re-seuille rotor/horizontal/vertical/turbulence
  au seuil courant de l'IHM. Les anciens/compacts `.sillage` restent lisibles mais leurs seuils
  restent figes.
- La sauvegarde demande maintenant le type: re-analysable (plus lourd, seuils modifiables) ou compact
  (plus leger, seuils figes).
- Les dossiers de staging sauvegarde/ouverture (`sillage_save_*`, `sillage_open_*`) sont crees sous
  le `tmp` configure du projet, pas dans le temp Windows global.
- Estimation mesuree sur les sauvegardes existantes: re-analysable probablement ~2.5x a 6x la taille
  compacte, bien moins que conserver les `NINJAFOAM_*` complets.

Verification:
- Tests cibles `tests/test_auto.py tests/test_pass2.py` -> `59 passed`.
- Suite complete `.\.venv\Scripts\python.exe -m pytest -q` -> `93 passed`.
- `git diff --check` OK (warnings LF/CRLF Windows seulement).

### 2026-06-30 — Sliders de plages pour la visu 3D auto (Codex)

Contexte:
- Les controles 3D etaient encore des spinboxes generiques et certains parametres restaient visibles
  alors qu'ils ne servaient pas pour la representation active.

Realise:
- Remplacement par des range sliders masques/affiches selon la representation:
  rotor min/max, horizontale min/max, verticale avec deux plages degueulantes/ascendances,
  turbulence min/max.
- Les sliders pilotent le filtrage reel des cellules via `metric_range`, puis la couleur:
  horizontal centre sur 0 (jaune pale), vertical avec gap calme masque, rotor/turbulence masques sous
  le min et satures au-dessus du max.
- Les `.sillage` v2 re-analysables re-seuillent depuis les scalaires sauvegardes; les anciens compacts
  restent bornes par leurs volumes deja extraits.

Verification:
- Couvert par la suite complete du 2026-07-01 ci-dessous.

### 2026-07-01 — Rattrapage consignation: defaut CPU + nettoyage `.vtu` auto (Codex)

Contexte:
- Les modifs etaient deja dans le code ou dans des docs techniques, mais pas assez clairement dans
  ce fichier de passation: Claude pouvait encore lire l'ancien defaut `min(4, coeurs)`.

Etat consigne:
- Defaut courant du slider **Calculs simultanes**: tous les coeurs physiques detectes.
- Le nombre effectif de calculs reste plafonne par le nombre de taches `domaines x heures`; le meme
  `momentum_parallel_plan(...)` alimente la ligne **Plan CPU** et les logs de lancement.
- Le nettoyage auto cible maintenant `z*.vtu`, pas seulement `z*_rotor.vtu`, pour couvrir volumes
  compacts rotor/horizontal/vertical/turbulence et sources `.sillage` v2 re-analysables.
- L'entree "Revue/consolidation mode auto" du 2026-06-26 est annotee comme historique pour eviter
  de reprendre l'ancien defaut prudent par erreur.

Verification:
- Rattrapage documentation uniquement; comportement couvert par les tests du 2026-07-01:
  auto/pass2 `60 passed`, suite complete `94 passed`.

### 2026-07-01 — Option topo 1 m IGN dans le mode auto (Codex)

Contexte:
- Demande: ajouter 1 m dans les possibilites topo du premier onglet si les donnees sont disponibles
  via IGN.

Realise:
- Combo topo auto: `1 m (IGN)`, `5 m`, `10 m`, `25 m`.
- `prepare_dem_ign` garde maintenant le fetch natif ~1 m pour une cible 1 m; avant, le lissage etait
  force a x2 et aurait ressorti une grille ~2 m.
- Les configs sauvegardees/restaurees choisissent le preset topo le plus proche.

Verification:
- Tests cibles `tests/test_auto.py tests/test_pass2.py` -> `60 passed`.
- Suite complete `.\.venv\Scripts\python.exe -m pytest -q` -> `94 passed`.
- `git diff --check` OK (warnings LF/CRLF Windows seulement).

### 2026-07-04 — Nouvelle IHM globale SousLeVent (Codex)

Contexte:
- Demande: unifier les deux anciennes versions en une nouvelle version globale appelee
  **SousLeVent**, avec les anciennes gardees en backup.

Realise:
- Nouveau module `src/sillage/souslevent/window.py` + script `scripts/souslevent.py` + entree
  console `souslevent`.
- Les anciennes fenetres restent lanceables: `sillage-gui` / `scripts/sillage_gui.py` et
  `sillage-auto` / `scripts/sillage_auto.py`.
- Onglet 1 unifie: menu deroulant **Selection = Parcours / Rectangle**.
- Menu deroulant **Calcul** avec les 3 options:
  1. `Pass-1 seul puis selection manuelle`: lance le criblage et remplit un onglet candidats;
     la selection manuelle v1 se fait dans une liste de domaines candidats.
  2. `Pass-1 + candidats multiples auto`: reutilise `run_auto(domain_mode="features")`.
  3. `Pass-2 partout`: route -> pavage corridor existant; rectangle -> pavage quadtree
     `partition_zone` pour couvrir toute la zone.
- Refactor pipeline: extraction d'un plan commun DEM + domaines; ajout `screen_candidates(...)`,
  `ScreeningResult`, et `AutoConfig.manual_zones` pour relancer Pass-2 sur les candidats choisis.

Limite connue:
- Limite initiale corrigee ensuite: la selection manuelle v1 etait une liste de candidats;
  l'entree "Carte cliquable des candidats Pass-1" ci-dessous ajoute la carte de selection.

Verification:
- Import/construction GUI offscreen `SousLeVentWindow` OK.
- Tests cibles sans `tmp_path`: `3 passed`.
- Les tests complets sont bloques par un probleme de permissions pytest sur le dossier temporaire
  `pytest-of-a2Kc` (hors assert applicatif); a relancer apres nettoyage du temp pytest Windows.

### 2026-07-04 — Correctif Pass-1 seule: fin de criblage (Codex)

Contexte:
- Retour test: en mode `Pass-1 seul puis selection manuelle`, l'IHM affichait:
  `TypeError: slice indices must be integers or None or have an __index__ method`.
- Le log utilisateur montrait que les 6 features etaient detectees, puis que l'erreur arrivait
  apres `Criblage termine : 6 candidat(s).`

Realise:
- Cause effective corrigee dans `src/sillage/auto/pipeline.py`:
  - `screen_candidates()` appelait `plan.timings.summary("Pass-1")`;
  - or `RunTimings.summary()` attend un entier `max_items`, donc Python tentait
    `items[:"Pass-1"]`;
  - remplacement par `plan.timings.summary()` et ajout d'un test de regression.
- Correction defensive dans `src/sillage/auto/partition.py`:
  - conversion explicite des `Candidate.row/col` en indices entiers avant slicing du MNT;
  - clamp de toutes les fenetres pixel candidat/source a l'emprise du MNT;
  - prevention du wrap negatif deja traite cote `corridor_tiles`.
- Ajout d'un test de regression avec candidat dont `row/col` arrivent en float.

Verification:
- `ruff` OK sur `src/sillage/auto/pipeline.py`, `src/sillage/auto/partition.py` et
  `tests/test_auto.py`.
- Tests cibles:
  `test_feature_domains_coerces_candidate_indices`,
  `test_screen_candidates_builds_timing_summary`,
  `test_manual_mode_config_carries_selected_zones`,
  `test_souslevent_window_builds_offscreen` -> `4 passed`.

### 2026-07-04 — Carte cliquable des candidats Pass-1 (Codex)

Contexte:
- Retour test: le rendu apres `Pass-1 seul puis selection manuelle` etait trop minimaliste avec
  une simple liste; il faut une carte pour choisir les candidats.

Realise:
- `ScreeningResult` transporte maintenant aussi la grille Pass-1 (`hazard`) produite par le
  criblage.
- Onglet `Candidats Pass-1` refait:
  - carte Matplotlib avec hillshade MNT + overlay danger Pass-1;
  - trace du parcours superposee si le calcul vient d'un parcours;
  - rectangles candidats numerotes;
  - clic simple sur rectangle propose = selection/deselection;
  - drag-and-drop sur la carte = creation + selection d'un nouveau rectangle manuel;
  - clic droit sur la carte = vider la selection;
  - synchronisation carte <-> liste de detail.
- La liste reste a droite pour verifier centre, largeur, relief et estimation de cellules topo.

Verification:
- `ruff` OK sur les modules touches.
- Tests cibles:
  `test_screen_candidates_builds_timing_summary`,
  `test_feature_domains_coerces_candidate_indices`,
  `test_souslevent_window_builds_offscreen` -> `3 passed`.
- Smoke test offscreen: rendu d'une carte candidat synthetique OK.

### 2026-07-04 — Fond IGN sur rendu Pass-2 (Codex)

Contexte:
- Retour test: le resultat Pass-2 manque de fond de carte IGN pour se reperer facilement.

Realise:
- Onglet rendu 3D global: ajout d'un selecteur **Fond**, par defaut `IGN plan`, avec
  `IGN ortho`, `OpenStreetMap`, `OpenTopoMap`, `Aucun`.
- Le changement de fond re-render le resultat courant sans relancer le calcul.
- Correction du vrai point bloquant observe: `contextily`/`joblib` essayait de creer son cache dans
  `AppData\Local\Temp`, ce qui plante sur cette machine et faisait retomber le rendu 3D sur la
  coloration relief sans fond.
- Nouveau helper `viz.map2d.import_contextily()`:
  - utilise `SILLAGE_TMP_DIR/contextily` si accessible;
  - fallback `.tmp/contextily` workspace si le temp configure est verrouille;
  - neutralise le cache `tempfile` pendant l'import de contextily.
- Le helper est utilise par les fonds 2D, le drapage 3D et le fallback MNT Terrarium.

Verification:
- `import_contextily()` OK sur cet environnement Windows.
- Tests cibles:
  `test_drape_basemap_reprojects_tiles_to_terrain_crs`,
  `test_souslevent_window_builds_offscreen` -> `2 passed`.
- `ruff` OK.

### 2026-07-04 — Fond IGN sur resultat Pass-1 candidats (Codex)

Contexte:
- Clarification utilisateur: la demande de fond IGN concernait le resultat Pass-1 / carte des
  candidats, pas le rendu 3D Pass-2 qui fonctionnait deja correctement.

Realise:
- Onglet `Candidats Pass-1`: ajout d'un selecteur **Fond** par defaut sur `IGN plan`.
- La carte des candidats affiche maintenant le fond selectionne sous la heatmap danger Pass-1,
  puis le parcours et les rectangles candidats/manuels au-dessus.
- Si le fond de carte est indisponible (reseau/tuiles/contextily), repli sur l'ancien rendu
  hillshade MNT + danger Pass-1, sans bloquer la selection.
- Le changement fait par malentendu sur Pass-2 est conserve pour l'instant: c'est un selecteur
  d'affichage uniquement, sans impact calcul.

Verification:
- `ruff` OK sur les fichiers touches.
- Tests cibles:
  `test_souslevent_window_builds_offscreen`,
  `test_souslevent_manual_candidate_rectangle_offscreen`,
  `test_souslevent_pass1_candidate_map_uses_basemap` -> `3 passed`.

### 2026-07-04 — Vent manuel homogene par plages vitesse/direction (Codex)

Contexte:
- Besoin utilisateur dans l'onglet selection parcours/creneau:
  - soit utiliser le vent meteo du creneau selectionne;
  - soit imposer un vent homogene sur toute la carte;
  - lancer un calcul pour chaque pas de vitesse et chaque pas de direction selectionnes.

Realise:
- Ajout d'un choix `Vent : Meteo du creneau / Homogene manuel` dans SousLeVent.
- En mode manuel:
  - slider de plage vitesses, pas force a 5 km/h;
  - slider de plage directions, pas force a 45 degres;
  - le slider de creneau meteo est desactive pour eviter l'ambiguite;
  - le plan CPU compte maintenant les scenarios `vitesse x direction` au lieu des heures.
- Le pipeline auto supporte `wind_mode="manual_grid"`:
  - chaque couple vitesse/direction devient un scenario de calcul;
  - les labels de resultat affichent `15 km/h · Ouest` plutot qu'une fausse heure;
  - les fleches de vent 3D du parcours suivent le scenario manuel affiche.
- Les champs de vent manuel sont sauvegardes/restaures dans les `.sillage`.

Note:
- Le criblage Pass-1 utilise pour l'instant le premier scenario manuel comme vent representatif
  pour detecter les candidats; la Pass-2 calcule ensuite tous les couples vitesse/direction sur
  les domaines selectionnes/automatiques.

Verification:
- `ruff` OK sur les fichiers touches.
- Tests cibles:
  `test_manual_wind_grid_scenarios_and_provider`,
  `test_souslevent_manual_wind_grid_config_offscreen`,
  `test_souslevent_window_builds_offscreen` -> `3 passed`.

### 2026-07-04 — Reorganisation logique onglet 1 (Codex)

Contexte:
- Demande utilisateur: ordonner l'onglet 1 de haut en bas selon le flux naturel:
  selection parcours/rectangle, carte IGN, vent, mode de calcul, validation centree, log en bas.

Realise:
- Onglet `Selection + calcul` reorganise:
  - menu `Selection` seul en haut;
  - carte IGN juste dessous;
  - bloc `Vent` ensuite, avec masquage complet du bloc non selectionne:
    - mode meteo: creneau + graduations + source prevision;
    - mode manuel: sliders vitesse/direction uniquement;
  - bloc `Calcul` ensuite, avec masquage des parametres inutiles:
    - marge corridor visible seulement en mode parcours;
    - candidats max visible pour Pass-1 / Pass-1 + auto;
    - pas secteurs visible seulement pour Pass-2 partout;
  - bouton `Valider` plus large et centre;
  - avancement + log tout en bas.
- Ajout d'assertions de regression UI offscreen sur les blocs visibles/masques.

Verification:
- `ruff` OK sur `src/sillage/souslevent/window.py` et `tests/test_pass2.py`.
- Tests cibles:
  `test_souslevent_window_builds_offscreen`,
  `test_souslevent_manual_wind_grid_config_offscreen` -> `2 passed`.

### 2026-07-04 — Verification parallelisme Pass-2 vent manuel (Codex)

Contexte:
- Retour utilisateur: les Pass-2 avec vent manuel ne semblaient pas se lancer en parallele quand
  plusieurs vitesses/orientations etaient selectionnees.

Realise:
- Ajout d'une ligne de plan Pass-2 dans le resume candidats et dans le log au lancement:
  `N domaines x M scenarios = K calculs Pass-2 · W en parallele x T thread(s)`.
- Ajout d'un test de regression qui simule quatre scenarios de vent manuel et verifie que les faux
  `run_momentum` se chevauchent bien. Si le pipeline repassait en sequentiel, ce test echouerait.

Conclusion:
- Le pipeline lance bien les scenarios vent manuel via le meme `ThreadPoolExecutor` que les heures
  meteo.
- Si l'IHM affiche `1 en parallele`, il faut regarder le slider `Calculs simultanes`, le nombre de
  coeurs detectes, ou le nombre reel de taches disponibles.

Verification:
- `ruff` OK sur les fichiers touches.
- Tests cibles:
  `test_run_auto_parallelizes_manual_wind_scenarios`,
  `test_manual_wind_grid_scenarios_and_provider`,
  `test_souslevent_manual_wind_grid_config_offscreen` -> `3 passed`.

### 2026-07-04 — Directions de vent en libelles cardinaux (Codex)

Contexte:
- Demande utilisateur: afficher les orientations de vent sous forme lisible (`Nord`, `Ouest`,
  `Nord-Est`, etc.) plutot que par valeur d'angle.

Realise:
- Ajout du helper commun `wind.directions.direction_label()`.
- Labels scenarios vent manuel:
  - avant: `10 km/h · 270°`;
  - maintenant: `10 km/h · Ouest`.
- Le slider de directions manuel affiche maintenant `Ouest -> Nord-Ouest` au lieu de `270 -> 315°`.
- Les messages de progression Pass-2 et la boussole/fleche vent 3D utilisent aussi ces libelles.
- Les degres restent stockes/envoyes a WindNinja en interne.

Verification:
- `ruff` OK sur les fichiers touches.
- Tests cibles:
  `test_manual_wind_grid_scenarios_and_provider`,
  `test_souslevent_manual_wind_grid_config_offscreen`,
  `test_add_compass_adds_north_and_wind_arrows` -> `3 passed`.

### 2026-07-04 — Deux sliders rendu 3D pour vent manuel (Codex)

Contexte:
- Demande utilisateur: dans l'onglet rendu 3D, ajouter un deuxieme slider pour les orientations
  lorsque le resultat vient d'un balayage de vent manuel.

Realise:
- Mode meteo: conservation du slider unique `Creneau`.
- Mode vent manuel: masquage du slider creneau et affichage de deux sliders separes:
  - `Vitesse`;
  - `Orientation`.
- Le couple vitesse/orientation selectionne rend directement le scenario correspondant.
- Le libelle courant reste combine, par exemple `20 km/h · Nord-Ouest`.
- Les changements de metrique, fond de carte et seuils utilisent le couple vitesse/orientation
  courant au lieu du slider creneau masque.

Verification:
- `ruff` OK sur les fichiers touches.
- Tests cibles:
  `test_souslevent_manual_wind_result_uses_two_render_sliders`,
  `test_souslevent_manual_wind_grid_config_offscreen`,
  `test_souslevent_window_builds_offscreen` -> `3 passed`.

### 2026-07-05 — Rendu 3D applique seulement apres validation (Codex)

Contexte:
- Retour utilisateur: dans l'onglet rendu 3D, deplacer les sliders d'heure, vitesse ou orientation
  relancait directement le rendu, ce qui pouvait freezer l'IHM quand le recalcul etait long.
- La legende etait trop proche des infos de selection et pouvait etre confondue avec les reglages.

Realise:
- Deplacement des controles de cas dans le panneau de reglages de rendu a droite:
  - mode meteo: slider `Creneau`;
  - mode vent manuel: sliders `Vitesse` et `Orientation`.
- Les changements de creneau/vitesse/orientation ne reconstruisent plus la scene: ils mettent
  seulement a jour le libelle du cas courant.
- Le recalcul 3D est maintenant declenche par le bouton `Appliquer le rendu`.
- Les changements de representation et de fond de carte attendent aussi ce bouton, pour pouvoir
  ajuster plusieurs reglages avant un seul rebuild.
- L'opacite reste live: elle ne reconstruit pas la scene, elle modifie seulement les acteurs deja
  affiches.
- Les legendes couleur/vent sont deplacees tout en bas du panneau de droite.

Verification:
- `.\.venv\Scripts\ruff.exe check --no-cache src/sillage/auto/window.py tests/test_pass2.py`
  -> OK.
- Tests cibles:
  `test_souslevent_window_builds_offscreen`,
  `test_souslevent_manual_wind_result_uses_two_render_sliders`,
  `test_souslevent_forecast_hour_slider_waits_for_apply` -> `3 passed`.
- Avertissement pytest connu et non bloquant: `.pytest_cache` / `WinError 183`.

### 2026-07-05 — Ordre du panneau rendu 3D corrige (Codex)

Contexte:
- Retour utilisateur: la legende couleur liee aux parametres de rendu (`Rotor`, vitesses,
  turbulence) doit rester au-dessus des sliders correspondants, et le selecteur de fond de carte
  doit etre tout en haut.

Realise:
- `Fond` de carte deplace tout en haut du panneau de droite.
- Legende couleur de la metrique de rendu replacee entre `Representation` et les sliders de seuils.
- Legende vent conservee en bas, car elle n'est pas liee aux sliders de seuils/metrique.
- Ajout d'un test offscreen qui verifie l'ordre du layout.

Verification:
- `.\.venv\Scripts\ruff.exe check --no-cache src/sillage/auto/window.py tests/test_pass2.py`
  -> OK.
- Tests cibles:
  `test_souslevent_window_builds_offscreen`,
  `test_souslevent_manual_wind_result_uses_two_render_sliders`,
  `test_souslevent_forecast_hour_slider_waits_for_apply` -> `3 passed`.
- Avertissement pytest connu et non bloquant: `.pytest_cache` / `WinError 183`.

### 2026-07-05 — Bouton explicite de recalcul vue 3D (Codex)

Contexte:
- Demande utilisateur: rendre le bouton d'application des sliders plus visible, avec le meme style
  vert que les boutons de lancement de calcul, et empecher les doubles clics pendant le recalcul.

Realise:
- Bouton renomme en `Recalculer la vue 3D`.
- Style vert applique via le meme QSS que `Valider` / lancement de calcul.
- Pendant le rebuild 3D synchrone:
  - bouton desactive;
  - texte `Calcul en cours...`;
  - barre de statut `Calcul en cours...`;
  - restauration du texte et de l'etat actif a la fin, avec statut `Vue 3D recalculee`.

Verification:
- `.\.venv\Scripts\ruff.exe check --no-cache src/sillage/auto/window.py tests/test_pass2.py`
  -> OK.
- Tests cibles:
  `test_souslevent_window_builds_offscreen`,
  `test_souslevent_manual_wind_result_uses_two_render_sliders`,
  `test_souslevent_forecast_hour_slider_waits_for_apply` -> `3 passed`.
- Avertissement pytest connu et non bloquant: `.pytest_cache` / `WinError 183`.

### 2026-07-11 — Gros runs complets et bord de corridor protege (Codex)

Contexte:
- Revue du nouveau pavage global puis retour reel: 67 secteurs demandes, seulement 18 cas affiches
  et un echec.
- Cause confirmee: le seuil historique de 3 Go annulait globalement toutes les taches restantes.

Realise:
- Le seuil de 3 Go devient un avertissement non bloquant; il ne tronque plus les lots.
- Chaque echec parallele est retente seul, puis rattache a sa zone/heure s'il persiste.
- Verification finale que chaque tache demandee produit soit un cas, soit un echec explicite.
- Message final sous la forme `cas reussis / cas attendus`.
- Derniere ligne/colonne du pavage recalee dans l'emprise: tampon OpenFOAM de 1,2 km conserve.
- Les candidats Pass-1 gardent leur taille physique liee au relief; plus de reduction a 400 m.
- Estimation CPU basee sur le vrai pas de grille apres plafonnement maillage.
- Adaptation topo Grossier > 25 m sans boucle de re-pavage.
- Libelles de l'apercu corriges: `secteurs de pavage`, pas `candidats Pass-1`.

Verification:
- 6 tests de regression cibles passes.
- `ruff check .` OK.
- Suite complete: **123 tests passes**, un warning matplotlib `tight_layout` connu et non bloquant.
