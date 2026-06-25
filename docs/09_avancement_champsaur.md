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
