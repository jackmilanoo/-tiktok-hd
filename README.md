# TikTok HD Mobile

Application web mobile Flask + yt-dlp.

## Déploiement Render
1. Crée un dépôt GitHub et ajoute tout le contenu de ce dossier.
2. Sur Render : New > Blueprint.
3. Connecte le dépôt GitHub.
4. Render détectera `render.yaml`.
5. Lance le déploiement.
6. Quand le service est prêt, ouvre l'URL `*.onrender.com` sur l'iPhone.

Le Dockerfile installe FFmpeg, nécessaire lorsque la meilleure vidéo et le meilleur audio doivent être fusionnés.

## iPhone
Dans Safari :
Partager > Sur l'écran d'accueil.

Cela donne une icône qui ouvre le site comme une application.

## Important
TikTok change régulièrement son fonctionnement. Si nécessaire, redéploie l'application afin de récupérer la dernière version de `yt-dlp`.

Utilise ce programme uniquement pour des vidéos que tu as le droit de télécharger.
