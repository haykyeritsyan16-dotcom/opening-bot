"""Hourly opening-exercise bot. Finds 'only move' opening positions with Stockfish,
saves them to bank.json (the phone app reads it) and sends one to Telegram."""
import chess, chess.engine, chess.pgn, io, json, os, random, sys, time, datetime
import urllib.request, urllib.parse

SF = os.environ.get("STOCKFISH", "/usr/games/stockfish")
BUDGET = float(os.environ.get("TIME_BUDGET_MIN", "9")) * 60      # minutes of searching per run
WANT = int(os.environ.get("WANT", "6"))                           # new positions per run
TZ = int(os.environ.get("LOCAL_UTC_OFFSET", "4"))                 # Armenia = UTC+4
QUIET = (23, 8)                                                   # no Telegram from 23:00 to 08:00 local
BANK_MAX = 400
USERS = ["DrNykterstein", "nihalsarin2004", "penguingim1", "Fins", "LyonBeast", "alireza2003", "Msb2",
         "Zhigalko_Sergei", "GMWSO", "opperwezen", "RebeccaHarris", "AnishGiri", "Arka50", "chessbrahs"]
LO, HI, GAP = int(os.environ.get("LO","-30")), int(os.environ.get("HI","70")), int(os.environ.get("GAP","55"))                                         # centipawns, same rules as the app


def http(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "opening-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def games_from_lichess():
    out = []
    for u in random.sample(USERS, 3):
        try:
            t = http("https://lichess.org/api/games/user/%s?max=40&rated=true&perfType=classical,rapid,blitz&moves=true&tags=true&clocks=false&evals=false&opening=false" % u).decode("utf8", "ignore")
            f = io.StringIO(t)
            while True:
                g = chess.pgn.read_game(f)
                if g is None: break
                out.append(g)
        except Exception as e:
            print("lichess", u, "failed:", e)
    return out


def candidates(games):
    c = []
    for g in games:
        moves = list(g.mainline_moves())
        if len(moves) < 30: continue
        for ply in random.sample(range(8, 25), 4):                    # a few different moments of each game
            b = g.board()
            for m in moves[:ply]: b.push(m)
            if b.is_check() or b.is_game_over(): continue
            c.append((b, g))
    random.shuffle(c)
    return c


def score(info, turn):
    return info["score"].pov(turn).score(mate_score=10000)


def too_easy(b, mv):
    return b.is_capture(mv) or b.gives_check(mv) or mv.promotion is not None or b.is_check()


def judge(eng, b, secs):
    infos = eng.analyse(b, chess.engine.Limit(time=secs), multipv=2)
    if len(infos) < 2 or "pv" not in infos[0] or "pv" not in infos[1]: return None
    return score(infos[0], b.turn), score(infos[1], b.turn), infos[0]["pv"][0]


def find(eng, cands, seen, deadline, want):
    found = []
    for b, g in cands:
        if time.time() > deadline or len(found) >= want: break
        key = " ".join(b.fen().split(" ")[:4])
        if key in seen: continue
        q = judge(eng, b, 0.4)                                        # quick look
        if not q or not (LO - 25 <= q[0] <= HI + 25) or q[0] - q[1] < GAP - 30: continue
        f = judge(eng, b, 6)                                          # careful look
        if not f or abs(f[0]) > 9000: continue
        best, second, mv = f
        if not (LO <= best <= HI) or best - second < GAP or abs(best - q[0]) > 60: continue
        if too_easy(b, mv): continue
        gap = (best - second) / 100
        kind = "equality" if best <= 30 else "better"
        stm = "w" if b.turn else "b"
        found.append({
            "id": "g:" + key.replace(" ", "_"), "fen": b.fen(), "my": stm, "me_cp": best, "no": b.fullmove_number,
            "opp": "%s – %s" % (g.headers.get("White", "?"), g.headers.get("Black", "?")), "date": "Lichess",
            "bc": True, "kind": "opening", "ver": True,
            "crit": ("only one move keeps the equality" if kind == "equality" else "only one move keeps you better") +
                    ". The next best move is %.2f worse. Losing 0.3 or more counts as a miss." % gap,
            "answer_san": b.san(mv), "answer_cp": best, "gap": round(gap, 2), "sent": False, "added": int(time.time()),
        })
        seen.add(key)
        print("found:", b.fen(), b.san(mv), best, "gap", gap)
    return found


def tg(token, method, **kw):
    data = urllib.parse.urlencode(kw).encode()
    return json.loads(http("https://api.telegram.org/bot%s/%s" % (token, method), data))


def chat_id(token):
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if cid: return cid
    r = tg(token, "getUpdates")
    for u in reversed(r.get("result", [])):
        m = u.get("message") or u.get("channel_post")
        if m: return str(m["chat"]["id"])
    return None


def send(token, cid, p):
    side = "White" if p["my"] == "w" else "Black"
    fenq = urllib.parse.quote(p["fen"], safe="")
    img = "https://lichess1.org/export/fen.png?fen=%s&color=%s&theme=brown&piece=cburnett" % (fenq, "white" if p["my"] == "w" else "black")
    link = "https://lichess.org/analysis/" + p["fen"].replace(" ", "_")
    cap = ("📖 <b>Opening • %s to move</b>\n%s.\n\n<tg-spoiler>Answer: %s (%+.2f). The next best move is %.2f worse.</tg-spoiler>\n\n<a href=\"%s\">Analyse on Lichess</a>"
           % (side, p["crit"].split(". ")[0].capitalize(), p["answer_san"], p["answer_cp"] / 100, p["gap"], link))
    try:
        tg(token, "sendPhoto", chat_id=cid, photo=img, caption=cap, parse_mode="HTML")
    except Exception as e:
        print("photo failed, sending text:", e)
        tg(token, "sendMessage", chat_id=cid, text=cap + "\n\nFEN: " + p["fen"], parse_mode="HTML")


def main():
    bank = json.load(open("bank.json")) if os.path.exists("bank.json") else []
    state = json.load(open("state.json")) if os.path.exists("state.json") else {}
    seen = set(" ".join(x["fen"].split(" ")[:4]) for x in bank)
    if len(sys.argv) > 2 and sys.argv[1] == "--pgn":                  # offline test
        f = open(sys.argv[2]); games = []
        while True:
            g = chess.pgn.read_game(f)
            if g is None: break
            games.append(g)
    else:
        games = games_from_lichess()
    print("games:", len(games))
    if games and len(bank) < BANK_MAX + 100:
        eng = chess.engine.SimpleEngine.popen_uci(SF)
        try:
            eng.configure({"Threads": int(os.environ.get("THREADS", "4")), "Hash": 256})
            bank += find(eng, candidates(games), seen, time.time() + BUDGET, WANT)
        finally:
            eng.quit()
    bank = bank[-BANK_MAX:]

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=TZ)
    quiet = now.hour >= QUIET[0] or now.hour < QUIET[1]
    if token and not quiet:
        today = now.strftime("%Y-%m-%d")
        n = 3 if state.get("morning") != today else 1                 # first message of the day: 3 positions
        state["morning"] = today
        cid = chat_id(token)
        if not cid: print("No Telegram chat yet: send any message to your bot first.")
        else:
            for p in [x for x in bank if not x.get("sent")][:n]:
                try: send(token, cid, p); p["sent"] = True
                except Exception as e: print("telegram failed:", e); break
    json.dump(bank, open("bank.json", "w"), ensure_ascii=False, indent=0)
    json.dump(state, open("state.json", "w"))
    print("bank:", len(bank), "unsent:", len([x for x in bank if not x.get("sent")]))


if __name__ == "__main__":
    main()
