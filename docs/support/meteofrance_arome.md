# Météo-France AROME (1.3 km) — accès API, clé, renouvellement

> Source de prévision **fine** pour Sillage (AROME 1.3 km sur la France), via l'API
> Météo-France. Complète/affine le vent libre par rapport à Open-Meteo (~11 km). L'ingestion
> GRIB (cfgrib) reste à faire (roadmap M4) ; pour l'instant on a **la clé + sa validation**.

## Le modèle

- **AROME** = modèle Météo-France **à échelle convective** (non-hydrostatique, *convection
  permitting*), **maille 1.3 km** sur la France métropolitaine. API : **`/public/arome/1.0`**
  (résolution 0.01°). Format livré : **GRIB2**.
- Pour comparaison : **ICON-D2** (DWD) = même classe (convection permitting, ~2.2 km), mais
  **ouvert sans clé** ; **HRRR** (NOAA, 3 km, USA). Le ~11 km d'Open-Meteo est de la classe
  modèle **global** (ICON 13 km / ECMWF / GFS).

## Informations de connexion (compte)

| | |
|---|---|
| **Portail** | <https://portail-api.meteofrance.fr> |
| **Compte (login)** | À conserver hors Git (`METEOFRANCE_ACCOUNT_LOGIN` dans `.env` si besoin pour l'IHM) |
| **E-mail associé** | À conserver hors Git (`METEOFRANCE_ACCOUNT_EMAIL` dans `.env` si besoin pour l'IHM) |
| **Application** | Application configurée sur le portail Météo-France |
| **API abonnée** | `AROME` — contexte `/public/arome/1.0`, tier `50PerMin` |
| **Type de credential** | `apiKey` (clé longue durée, en-tête `apikey:`) |

## La clé

- **Elle n'est PAS committée.** Elle vit uniquement dans le fichier **`.env`** à la racine
  (gitignoré), variable **`METEOFRANCE_API_KEY`**. `config.load_config()` la lit dans
  `Config.meteofrance_api_key`.
- Les infos de compte portail ne sont pas committées non plus. Pour les afficher dans la popup
  de renouvellement locale, ajouter éventuellement dans `.env` :
  ```
  METEOFRANCE_ACCOUNT_LOGIN=<login_portail>
  METEOFRANCE_ACCOUNT_EMAIL=<email_compte>
  ```
- La validité de la clé courante est détectée automatiquement hors-ligne via son payload JWT.
- C'est un **JWT** : la date d'expiration (`exp`) et les API abonnées (`subscribedAPIs`) sont
  lisibles **hors-ligne** dans sa charge utile — c'est ce que Sillage décode pour la valider.

### Validation dans l'app

`sillage.wind.meteofrance.check_arome_key(token)` renvoie un `KeyStatus`
(`ok / missing / malformed / expired / not_subscribed / expiring_soon`). À l'ouverture,
`MainWindow._check_meteofrance_key()` :

- **clé absente** → silencieux (AROME est optionnel, Open-Meteo par défaut) ;
- **clé valide** → message discret en barre d'état ;
- **clé invalide / expirée / expirant sous 30 j** → **popup** d'avertissement avec la
  procédure de renouvellement ci-dessous.

## Procédure de renouvellement (quand la clé expire ou est révoquée)

1. Se connecter au **portail** <https://portail-api.meteofrance.fr> avec le compte propriétaire
   de la clé (infos hors Git, éventuellement dans `.env`).
2. Vérifier (ou rétablir) l'**abonnement** à l'API **« AROME »** (`/public/arome/1.0`).
3. Ouvrir l'application de clés API configurée sur le portail → onglet des **clés API**
   (clés *PRODUCTION*).
4. **Générer** une nouvelle clé (le portail permet une validité **jusqu'à 3 ans** ; choisir la
   plus longue).
5. Coller la clé dans **`.env`** :
   ```
   METEOFRANCE_API_KEY=<nouvelle_clé>
   ```
6. **Redémarrer Sillage** (le `.env` est relu au démarrage).

> Rappel jetons : un **jeton OAuth2** (`/token`) ne vit que **1 h** ; on utilise donc une
> **clé API** (apiKey), valable jusqu'à 3 ans, pour éviter tout rafraîchissement.

## À faire ensuite (non couvert ici)

- Provider `wind/` lisant les paquets **GRIB2 AROME** (cfgrib/eccodes), sélection des niveaux
  de pression à hauteur de crête → vent fin pour le criblage Pass-1 et la CL du Pass-2.
- Alternative sans clé pour prototyper : **meteo.data.gouv.fr** (paquets AROME en open data).
