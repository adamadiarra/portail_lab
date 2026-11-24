"""Test d'intégration avec un vrai Docker (image locale `labtest:1` servant HTTP sur 8080)."""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
d = tempfile.mkdtemp(); env = dict(os.environ, DB_PATH=d + "/t.db", ADMIN_PASSWORD="AdminPass1", INVITE_CODE="inv")
srv = subprocess.Popen([sys.executable, "portail_labs.py", "--port", "8125"], env=env, stdout=subprocess.DEVNULL); time.sleep(1.5)
B = "http://127.0.0.1:8125"; fails = 0
class S:
    ck = None
    def req(s, m, p, body=None):
        r = urllib.request.Request(B + p, data=None if body is None else json.dumps(body).encode(), method=m, headers={"Content-Type": "application/json"})
        if s.ck: r.add_header("Cookie", s.ck)
        try: resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e: resp = e
        sc = resp.headers.get("Set-Cookie")
        if sc: s.ck = sc.split(";")[0]
        return resp.status, json.loads(resp.read() or b"{}")
def ok(c, m):
    global fails; print("OK  " if c else "FAIL", m); fails += (not c)
def http(port):
    try: return urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).status
    except Exception: return None
def wait(fn, t=25):
    e = time.time() + t
    while time.time() < e:
        v = fn()
        if v: return v
        time.sleep(0.5)
def containers(): return subprocess.run(["docker", "ps", "-a", "--filter", "label=portail=1", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
try:
    a = S(); a.req("POST", "/api/login", {"username": "admin", "password": "AdminPass1"})
    sc, j = a.req("POST", "/api/admin/labs", {"title": "Web", "capacity": 3, "port_base": 18000, "host": "127.0.0.1", "image": "labtest:1", "ports": "web:8080", "instructions": "http://127.0.0.1:{port_web}/ pour {user}"})
    ok(sc == 200, f"lab avec image créé ({j})")
    sc, j = a.req("POST", "/api/admin/labs", {"title": "Mauvais", "capacity": 3, "image": "labtest:1", "ports": "", "port_base": 0}); ok(sc == 400, "image sans port refusée")
    lid = a.req("GET", "/api/admin/labs")[1]["labs"][0]["id"]
    st = {}
    for n in ("ana", "ben"):
        st[n] = S(); st[n].req("POST", "/api/register", {"username": n, "password": "motdepasse1", "invite": "inv"})
    ok(st["ana"].req("POST", "/api/enroll", {"lab_id": lid})[0] == 200, "ana s'inscrit")
    ok(st["ben"].req("POST", "/api/enroll", {"lab_id": lid})[0] == 200, "ben s'inscrit")
    m = wait(lambda: (lambda x: x if x["status"] == "running" else None)(st["ana"].req("GET", "/api/my-lab")[1]["lab"]))
    ok(bool(m), "statut ana = running"); ok(m and m["ports"] == [{"name": "web", "port": 18001}] and "18001" in m["instructions"], "port 18001 (base+place 1) et variable {port_web}")
    ok(wait(lambda: http(18001) == 200) is True, "conteneur d'ana joignable sur 18001")
    ok(wait(lambda: http(18002) == 200) is True, "conteneur de ben joignable sur 18002 (isolé)")
    ok(sorted(containers()) == [f"lab-{lid}-u2", f"lab-{lid}-u3"], f"2 conteneurs : {containers()}")
    ok(st["ana"].req("POST", "/api/unenroll", {})[0] == 200, "ana quitte")
    ok(wait(lambda: http(18001) is None) is True, "port 18001 fermé : conteneur supprimé")
    ok(containers() == [f"lab-{lid}-u3"], "il ne reste que le conteneur de ben")
    # redémarrage
    subprocess.run(["docker", "rm", "-f", f"lab-{lid}-u3"], capture_output=True)
    ok(st["ben"].req("POST", "/api/my-lab/restart", {})[0] == 200, "ben redémarre son lab")
    ok(wait(lambda: http(18002) == 200) is True, "conteneur de ben recréé")
    # image introuvable -> statut error, sans planter
    a.req("POST", "/api/admin/labs", {"title": "Absent", "capacity": 2, "port_base": 19000, "image": "inexistante-xyz:9", "ports": "web:8080"})
    lid2 = [l for l in a.req("GET", "/api/admin/labs")[1]["labs"] if l["title"] == "Absent"][0]["id"]
    x = S(); x.req("POST", "/api/register", {"username": "zoe", "password": "motdepasse1", "invite": "inv"}); x.req("POST", "/api/enroll", {"lab_id": lid2})
    e = wait(lambda: (lambda y: y if y["status"] == "error" else None)(x.req("GET", "/api/my-lab")[1]["lab"]), 40)
    ok(bool(e), f"image introuvable -> statut error ({e and e['detail']})")
    # liste des images locales + vérification d'image
    im = a.req("GET", "/api/admin/images")[1]; ok(im["docker"] and "labtest:1" in im["images"], f"liste des images locales : {im['images']}")
    sc, j = a.req("POST", "/api/admin/images/check", {"image": "labtest:1"}); ok(sc == 200 and j["present"] and j["ports"] == [] and j["size_mb"] > 0, f"vérifier image présente : {j}")
    sc, j = a.req("POST", "/api/admin/images/check", {"image": "inexistante-xyz:9"}); ok(sc == 200 and j["present"] is False, "image absente détectée sans téléchargement")
    sc, j = a.req("POST", "/api/admin/images/check", {"image": "inexistante-xyz:9", "pull": True}); ok(sc == 404, f"téléchargement d'une image introuvable -> erreur claire ({j.get('error')})")
    ok(a.req("POST", "/api/admin/images/check", {"image": "bad image;rm"})[0] == 400, "nom d'image invalide refusé")
    ok(st["ana"].req("GET", "/api/admin/images")[0] == 403, "liste des images réservée à l'admin")
    subprocess.run(["docker", "tag", "labtest:1", "labports:1"], capture_output=True)
    # suppression du lab -> conteneurs supprimés
    a.req("POST", "/api/admin/labs/delete", {"id": lid})
    ok(wait(lambda: containers() == []) is True, "suppression du lab : conteneurs supprimés")
finally:
    srv.terminate()
    for n in containers(): subprocess.run(["docker", "rm", "-f", n], capture_output=True)
print("ÉCHECS :", fails); sys.exit(1 if fails else 0)
