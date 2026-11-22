import json, os, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
d = tempfile.mkdtemp(); env = dict(os.environ, DB_PATH=d + "/t.db", ADMIN_PASSWORD="AdminPass1", INVITE_CODE="inv123")
srv = subprocess.Popen([sys.executable, "portail_labs.py", "--port", "8123"], env=env, stdout=subprocess.DEVNULL); time.sleep(1.5)
B = "http://127.0.0.1:8123"; fails = 0
class S:
    def __init__(s): s.ck = None
    def req(s, m, p, body=None, ctype="application/json"):
        r = urllib.request.Request(B + p, data=None if body is None else json.dumps(body).encode(), method=m, headers={"Content-Type": ctype})
        if s.ck: r.add_header("Cookie", s.ck)
        try: resp = urllib.request.urlopen(r)
        except urllib.error.HTTPError as e: resp = e
        sc = resp.headers.get("Set-Cookie")
        if sc: s.ck = sc.split(";")[0] if "sid=;" not in sc else None
        return resp.status, json.loads(resp.read() or b"{}")
def ok(c, msg):
    global fails; print(("OK  " if c else "FAIL"), msg); fails += (not c)
try:
    a = S(); ok(a.req("POST", "/api/login", {"username": "admin", "password": "AdminPass1"})[0] == 200, "admin login")
    ok(a.req("POST", "/api/admin/labs", {"title": "Onduleurs", "capacity": 2, "port_base": 5020, "host": "h", "instructions": "user={user} slot={slot} port={port}"})[0] == 200, "créer lab (cap 2)")
    ok(a.req("POST", "/api/admin/labs", {"title": "Node-RED", "capacity": 5})[0] == 200, "créer lab 2")
    labs = a.req("GET", "/api/admin/labs")[1]["labs"]; l1 = [l for l in labs if l["title"] == "Onduleurs"][0]["id"]; l2 = [l for l in labs if l["title"] == "Node-RED"][0]["id"]
    s = {}
    for n in ("alice", "bob", "carl"):
        s[n] = S(); sc, j = s[n].req("POST", "/api/register", {"username": n, "password": "motdepasse1", "invite": "inv123"}); ok(sc == 200, f"inscription {n}")
    ok(S().req("POST", "/api/register", {"username": "eve", "password": "motdepasse1", "invite": "faux"})[0] == 403, "mauvais code d'invitation refusé")
    ok(S().req("GET", "/api/labs")[0] == 401, "API labs sans session = 401")
    ok(s["alice"].req("GET", "/api/admin/labs")[0] == 403, "étudiant refusé sur l'admin")
    ok(s["alice"].req("POST", "/api/admin/labs", {"title": "x"}, ctype="text/plain")[0] == 415, "POST non JSON refusé (CSRF)")
    ok(s["alice"].req("POST", "/api/enroll", {"lab_id": l1})[0] == 200, "alice s'inscrit au lab 1")
    sc, j = s["alice"].req("POST", "/api/enroll", {"lab_id": l2}); ok(sc == 409, "alice ne peut PAS s'inscrire à un 2e lab")
    j = s["alice"].req("GET", "/api/my-lab")[1]["lab"]; ok(j["slot"] == 1 and j["ports"][0]["port"] == 5021 and j["instructions"] == "user=alice slot=1 port=5021", "page Mon lab : place, port, variables")
    ok(s["bob"].req("POST", "/api/enroll", {"lab_id": l1})[0] == 200, "bob s'inscrit (place 2)")
    ok(s["carl"].req("POST", "/api/enroll", {"lab_id": l1})[0] == 409, "lab complet refusé")
    ok(s["alice"].req("POST", "/api/unenroll", {})[0] == 200, "alice quitte")
    ok(s["carl"].req("POST", "/api/enroll", {"lab_id": l1})[0] == 200, "carl prend la place libérée")
    ok(s["carl"].req("GET", "/api/my-lab")[1]["lab"]["slot"] == 1, "la place 1 est réattribuée")
    ok(s["alice"].req("POST", "/api/enroll", {"lab_id": l2})[0] == 200, "alice peut maintenant choisir un autre lab")
    # concurrence : 6 inscriptions simultanées sur un lab de capacité 2
    a.req("POST", "/api/admin/labs", {"title": "Course", "capacity": 2}); lc = [l for l in a.req("GET", "/api/admin/labs")[1]["labs"] if l["title"] == "Course"][0]["id"]
    ss = []
    for i in range(6):
        x = S(); x.req("POST", "/api/register", {"username": f"u{i}00", "password": "motdepasse1", "invite": "inv123"}); ss.append(x)
    res = []; ts = [threading.Thread(target=lambda x=x: res.append(x.req("POST", "/api/enroll", {"lab_id": lc})[0])) for x in ss]
    [t.start() for t in ts]; [t.join() for t in ts]; ok(res.count(200) == 2 and res.count(409) == 4, f"concurrence : 2 acceptées / 4 refusées ({res})")
    ok(a.req("POST", "/api/admin/labs", {"id": lc, "title": "Course", "capacity": 1})[0] == 409, "capacité < places occupées refusée")
    ok(a.req("POST", "/api/admin/unenroll", {"user_id": 3})[0] == 200, "admin désinscrit un étudiant")
    for i in range(9): sc = S().req("POST", "/api/login", {"username": "alice", "password": "mauvais"})[0]
    ok(sc == 429, "anti brute-force (429)")
    ok(s["bob"].req("POST", "/api/logout", {})[0] == 200 and s["bob"].req("GET", "/api/labs")[0] == 401, "déconnexion")
finally:
    srv.terminate()
print("ÉCHECS :", fails); sys.exit(1 if fails else 0)
