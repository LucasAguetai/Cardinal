# Déployer Cardinal sur `cardinal.aguetai.fr` (VM Oracle Always Free)

Objectif : faire tourner Cardinal en continu sur une petite VM Linux gratuite,
accessible en HTTPS sur `cardinal.aguetai.fr`, protégé par un mot de passe.

Architecture : **Caddy** (HTTPS auto) → **gunicorn** (serveur Python) → Cardinal.
Le scheduler tourne dans le process gunicorn ; la base est le fichier `cardinal.db`.

---

## 1. Créer la VM Oracle (Always Free)

1. Console Oracle Cloud → *Compute* → *Instances* → *Create Instance*.
2. Image : **Ubuntu 22.04**. Shape : **Ampere A1 (ARM)** ou **VM.Standard.E2.1.Micro** (tout deux Always Free).
3. Ajoute ta **clé SSH** (pour te connecter).
4. Réseau : garde le VCN par défaut, **note l'IP publique** de l'instance.

### Ouvrir les ports 80 et 443 (⚠️ deux niveaux chez Oracle)

**a) Security List** (réseau Oracle) : VCN → Security Lists → Default → *Add Ingress Rules* :
- Source `0.0.0.0/0`, TCP, port **80**
- Source `0.0.0.0/0`, TCP, port **443**

**b) Pare-feu de la VM** (les images Oracle Ubuntu bloquent tout par défaut) — en SSH :
```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## 2. DNS : pointer le sous-domaine vers la VM

Chez le registrar qui gère **aguetai.fr**, ajoute un enregistrement :

| Type | Nom       | Valeur              |
|------|-----------|---------------------|
| A    | `cardinal`| `<IP publique VM>`  |

(Ton `lucas.aguetai.fr` reste inchangé — c'est un sous-domaine indépendant.)
Attends quelques minutes que le DNS se propage (`ping cardinal.aguetai.fr` doit renvoyer l'IP).

---

## 3. Installer Cardinal sur la VM

En SSH sur la VM :
```bash
sudo apt update && sudo apt install -y python3-venv git
sudo mkdir -p /opt/cardinal && sudo chown $USER /opt/cardinal
git clone <URL-de-ton-dépôt> /opt/cardinal      # ou copie les fichiers via scp
cd /opt/cardinal
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Configurer les secrets (`/opt/cardinal/.env`)
```ini
# Clés LLM (au moins une)
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
NVD_API_KEY=            # optionnel

# Production
CARDINAL_PASSWORD=choisis-un-mot-de-passe-solide   # active le login
CARDINAL_SECRET_KEY=colle-ici-une-longue-chaine-aleatoire
CARDINAL_SCHEDULER=1                               # démarre la veille auto sous gunicorn
```
Génère une clé secrète : `python3 -c "import secrets; print(secrets.token_hex(32))"`.

---

## 4. Service systemd (gunicorn, redémarrage auto)

Crée `/etc/systemd/system/cardinal.service` :
```ini
[Unit]
Description=Cardinal
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/cardinal
EnvironmentFile=/opt/cardinal/.env
# 1 SEUL worker = 1 seul scheduler. --threads pour la concurrence des requêtes.
ExecStart=/opt/cardinal/venv/bin/gunicorn -w 1 --threads 4 -b 127.0.0.1:8000 --timeout 120 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
Puis :
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cardinal
sudo systemctl status cardinal          # doit être "active (running)"
```
> ⚠️ **Un seul worker gunicorn** (`-w 1`) : sinon plusieurs schedulers tourneraient en parallèle.

---

## 5. Caddy : HTTPS automatique + reverse proxy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Édite `/etc/caddy/Caddyfile` (remplace tout le contenu par) :
```
cardinal.aguetai.fr {
    reverse_proxy 127.0.0.1:8000
}
```
Puis :
```bash
sudo systemctl restart caddy
```
Caddy obtient tout seul le certificat Let's Encrypt (le DNS doit déjà pointer sur la VM).

---

## 6. Vérifier

- Ouvre **https://cardinal.aguetai.fr** → page de connexion Cardinal.
- Entre `CARDINAL_PASSWORD` → dashboard.
- `curl https://cardinal.aguetai.fr/healthz` → `ok`.

## Mettre à jour plus tard
```bash
cd /opt/cardinal && git pull && ./venv/bin/pip install -r requirements.txt
sudo systemctl restart cardinal
```

## Sauvegarde
Toute la donnée est dans **`/opt/cardinal/cardinal.db`** — copie ce fichier pour sauvegarder.

---

### Notes
- **Sans** `CARDINAL_PASSWORD`, l'app n'a pas de login (mode local `python app.py`). En prod, définis-le **toujours** : sinon n'importe qui consommerait tes quotas.
- Le scheduler ne tourne que si `CARDINAL_SCHEDULER=1` (gunicorn). En local, `python app.py` le démarre tout seul.
- Quotas gratuits : un seul utilisateur (toi) reste dans les limites Gemini/Groq. Ne partage pas le mot de passe largement.
