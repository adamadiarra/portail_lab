# Portail des labs

Les étudiants s'inscrivent (code d'invitation), voient la liste des labs avec les places restantes,
s'inscrivent à **un seul lab à la fois**, puis consultent **Mon lab** (hôte, ports, état, instructions).
Si le lab a une image Docker, le portail **lance un conteneur dédié** à l'inscription et le **supprime** quand l'étudiant quitte le lab.

```
python portail_labs.py                      # http://127.0.0.1:8000  (aucune dépendance)
docker compose up -d --build                # portail dans Docker (monte le socket Docker)
python test_portail.py                      # tests API
python test_docker.py                       # tests avec un vrai Docker (image locale labtest:1)
```

Administrateur : créé au 1er démarrage (`ADMIN_USER`/`ADMIN_PASSWORD`, sinon mot de passe affiché dans les logs).
Onglet **Administration** : labs, code d'invitation, comptes.

## Lab avec conteneur
Champs : image, ports `nom:port` (ex. `web:8080,modbus:5020`), mémoire, CPU, variables d'environnement.
Port hôte de chaque port = **port de base + 1000 × rang + n° de place** (base 18000, place 3 : web 18003, modbus 19003).
Variables (instructions, URL, env) : `{user}`, `{slot}`, `{port}` (premier port), `{port_<nom>}`.
Chaque conteneur est limité (mémoire, CPU, 256 processus, `no-new-privileges`), redémarre avec la machine
et les conteneurs orphelins sont nettoyés au démarrage du portail. Ouvrir les ports (pare-feu) correspondants.

## Sécurité
- Derrière HTTPS (Caddy/Nginx) avec `COOKIE_SECURE=1`.
- Le socket Docker donne les droits root à qui contrôle le portail : protéger le compte admin, ne pas exposer d'autre service.
