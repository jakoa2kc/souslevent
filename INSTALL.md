# Installer SousLeVent (Windows)

Guide pour installer et lancer l'application **sans rien connaître à Python**.

## 1. Télécharger l'application

1. Va sur la page des versions : **<https://github.com/jakoa2kc/souslevent/releases/latest>**
2. Télécharge **`SousLeVent-1.0.0-windows.zip`** (~1 Go).
3. Décompresse-le où tu veux (ex. `C:\SousLeVent`) — clic droit → « Extraire tout… ».
4. Lance **`SousLeVent.exe`** dans le dossier extrait.

> ⚠️ **Windows SmartScreen** peut afficher « Windows a protégé votre ordinateur » (l'exe n'est pas
> signé) : clique **« Informations complémentaires »** puis **« Exécuter quand même »**.

À ce stade l'application s'ouvre : carte, sélection de parcours, prévisions de vent fonctionnent.
Pour lancer des **calculs** (Pass-1/Pass-2), il faut aussi WindNinja — étape suivante.

## 2. Installer WindNinja (le solveur — requis pour les calculs)

SousLeVent pilote **WindNinja** (logiciel gratuit du Missoula Fire Lab / US Forest Service) :

1. Télécharge l'installateur Windows ici : **<https://weather.firelab.org/windninja/>**
   (ou <https://github.com/firelab/windninja/releases>).
2. Installe-le normalement (version ≥ 3.10 ; **coche l'option momentum/OpenFOAM** si proposée).
3. Note le chemin de `WindNinja_cli.exe`, en général :
   `C:\Program Files\WindNinja\WindNinja-3.x.x\bin\WindNinja_cli.exe`

## 3. Configurer (fichier `.env` à côté de l'exe)

Dans le dossier où se trouve `SousLeVent.exe`, crée un fichier texte nommé **`.env`**
(attention : pas `.env.txt` !) avec ce contenu, en adaptant le chemin WindNinja :

```ini
WINDNINJA_CLI=C:\Program Files\WindNinja\WindNinja-3.11.0\bin\WindNinja_cli.exe

# Optionnel — clé API Météo-France AROME (sinon repli automatique sur Open-Meteo, sans clé) :
# METEOFRANCE_API_KEY=...

# Optionnel — dossier de travail (MNT, calculs, sorties). Défaut : C:\A2K\SousLeVent
# SILLAGE_GENERATED_ROOT=D:\SousLeVent
```

Le fichier peut aussi être placé dans `%APPDATA%\SousLeVent\.env` si tu préfères.

## 4. Premier lancement

- **Connexion internet requise** au premier usage d'une zone : téléchargement du relief (IGN) et
  des prévisions de vent (Open-Meteo/AROME). Tout est ensuite mis en cache.
- Trace un parcours (ou un rectangle), choisis le créneau, **Valider** → onglet candidats →
  sélectionne les zones → **Lancer Pass-2** → onglet 3D.
- Les calculs Pass-2 prennent de **quelques minutes à plus d'une heure** selon maillage × zones ×
  heures — l'application affiche l'estimation **avant** de lancer.

## Configuration minimale conseillée

- Windows 10/11 64 bits ; **CPU multi-cœurs** (le solveur est limité par le CPU, pas par le GPU) ;
- 16 Go de RAM conseillés (8 Go minimum) ;
- ~2 Go pour l'application + **10 Go et plus** d'espace libre pour le dossier de travail.

## Alternative pour utilisateurs Python

```bat
pip install https://github.com/jakoa2kc/souslevent/releases/latest/download/souslevent-1.0.0-py3-none-any.whl
souslevent
```
(Python ≥ 3.11 ; WindNinja et le `.env` restent nécessaires — voir étapes 2-3.)

## Problèmes fréquents

| Symptôme | Cause / solution |
|---|---|
| « WindNinja_cli introuvable » au lancement d'un calcul | Chemin `WINDNINJA_CLI` du `.env` faux — vérifie le chemin exact de ton installation |
| Pas de fond de carte / pas de vent | Pas d'accès internet (proxy/pare-feu) — la carte retombe sur l'ombrage du relief |
| Fenêtre bloquée « SmartScreen » | « Informations complémentaires » → « Exécuter quand même » |
| Calcul très long | Réduis le maillage (Grossier/Moyen), le nombre de zones ou d'heures — le coût est affiché avant lancement |

> ⚠️ **Rappel sécurité** : SousLeVent est une aide à la décision pédagogique (modèle RANS
> stationnaire). Il ne remplace ni les conditions réelles, ni ton jugement, ni ta formation.
