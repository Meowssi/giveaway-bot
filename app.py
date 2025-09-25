import os, re, json, threading, time, random
from datetime import datetime
from zoneinfo import ZoneInfo
import psycopg2
from dotenv import load_dotenv
from slack_bolt import App

load_dotenv()
app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "America/New_York"))
DATABASE_URL = os.environ["DATABASE_URL"]
BOT_USER_ID = app.client.auth_test()["user_id"]


def conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    c = conn()
    c.autocommit = True
    cur = c.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS giveaways(
      id SERIAL PRIMARY KEY,
      channel_id TEXT NOT NULL,
      message_ts TEXT NOT NULL,
      emoji TEXT NOT NULL,
      title TEXT NOT NULL,
      winners_count INTEGER NOT NULL DEFAULT 1,
      end_ts BIGINT NOT NULL,
      creator_user_id TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW(),
      UNIQUE(channel_id, message_ts)
    )"""
    )
    cur.close()
    c.close()


def db_execute(q, args=()):
    c = conn()
    c.autocommit = True
    cur = c.cursor()
    cur.execute(q, args)
    cur.close()
    c.close()


def db_query(q, args=()):
    c = conn()
    cur = c.cursor()
    cur.execute(q, args)
    rows = cur.fetchall()
    cur.close()
    c.close()
    return rows


def parse_duration(s):
    s = (s or "").strip().lower()
    total = 0
    for num, unit in re.findall(r"(\d+)\s*([dhm])", s):
        n = int(num)
        if unit == "d":
            total += n * 86400
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
    return total


def parse_args(text):
    winners = 1
    m = re.search(r"(?:^|\s)-w\s*(\d+)", text)
    if m:
        winners = max(1, min(50, int(m.group(1))))
        text = (text[: m.start()] + text[m.end() :]).strip()
    parts = text.split(maxsplit=1) if text else []
    dur = parts[0] if parts else ""
    title = parts[1] if len(parts) > 1 else ""
    return dur, title, winners


def slack_date(epoch):
    return f"<!date^{epoch}^*{{date_short_pretty}}* at *{{time}}* ({{timezone}})|{datetime.fromtimestamp(epoch, LOCAL_TZ).isoformat()}>"


def conclude_one(gid, channel_id, ts, emoji, title, winners_count):
    try:
        resp = app.client.reactions_get(channel=channel_id, timestamp=ts, full=True)
        users = []
        for r in (resp.get("message", {}) or {}).get("reactions", []) or []:
            if r.get("name") == emoji:
                users = r.get("users", []) or []
                break

        valid = []
        for u in set(users):
            if u == BOT_USER_ID:
                continue
            try:
                info = app.client.users_info(user=u)
                if not info.get("user", {}).get("is_bot", False):
                    valid.append(u)
            except Exception:
                pass

        winners_count = min(max(1, winners_count), len(valid)) if valid else 0

        if winners_count == 0:
            app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=ts,
                text=f"⏰ Giveaway ended — *{title}*. No valid entries.",
                reply_broadcast=True,
            )
        else:
            winners = random.sample(valid, winners_count)
            mentions = " ".join(f"<@{u}>" for u in winners)
            app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=ts,
                text=f"🎉 Giveaway ended — *{title}*\nWinners ({winners_count}/{len(valid)}): {mentions}",
                reply_broadcast=True,
            )

        db_execute(
            "UPDATE giveaways SET status='closed', updated_at=NOW() WHERE id=%s", (gid,)
        )
    except Exception as e:
        try:
            app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=ts,
                text=f"⚠️ Could not finish giveaway.\n`{e}`",
                reply_broadcast=True,
            )
        except Exception:
            pass
        db_execute(
            "UPDATE giveaways SET status='error', updated_at=NOW() WHERE id=%s", (gid,)
        )


def scheduler():
    while True:
        try:
            now = int(time.time())
            rows = db_query(
                "SELECT id, channel_id, message_ts, emoji, title, winners_count FROM giveaways WHERE status='open' AND end_ts <= %s LIMIT 25",
                (now,),
            )
            for row in rows:
                gid, ch, ts, emoji, title, wc = row
                updated = db_query(
                    "UPDATE giveaways SET status='processing', updated_at=NOW() WHERE id=%s AND status='open' RETURNING id",
                    (gid,),
                )
                if updated:
                    conclude_one(gid, ch, ts, emoji, title, wc)
        except Exception:
            pass
        time.sleep(15)


@app.command("/giveaway")
def handle_cmd(ack, body, command, client):
    ack()
    channel_id = body["channel_id"]
    text = (command.get("text") or "").strip()
    if text:
        dur_s, title, winners = parse_args(text)
        seconds = parse_duration(dur_s)
        if seconds > 0 and title:
            create_and_post(
                client, body["user_id"], channel_id, title, seconds, "tada", winners
            )
            return
    open_modal(client, body, channel_id)


def open_modal(client, body, channel_id):
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "giveaway_modal",
            "private_metadata": json.dumps({"channel_id": channel_id}),
            "title": {"type": "plain_text", "text": "New Giveaway"},
            "submit": {"type": "plain_text", "text": "Start"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "b_t",
                    "label": {"type": "plain_text", "text": "Title"},
                    "element": {"type": "plain_text_input", "action_id": "a_t"},
                },
                {
                    "type": "input",
                    "block_id": "b_d",
                    "label": {
                        "type": "plain_text",
                        "text": "Duration (e.g., 1d, 2h30m, 45m)",
                    },
                    "element": {"type": "plain_text_input", "action_id": "a_d"},
                },
                {
                    "type": "input",
                    "block_id": "b_w",
                    "label": {"type": "plain_text", "text": "Number of winners"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "a_w",
                        "initial_value": "1",
                    },
                },
                {
                    "type": "input",
                    "optional": True,
                    "block_id": "b_e",
                    "label": {
                        "type": "plain_text",
                        "text": "Emoji name (default: tada)",
                    },
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "a_e",
                        "initial_value": "tada",
                    },
                },
            ],
        },
    )


@app.view("giveaway_modal")
def submit_modal(ack, body, view, client):
    ack()
    vals = view["state"]["values"]
    title = vals["b_t"]["a_t"]["value"].strip()
    dur = vals["b_d"]["a_d"]["value"].strip()
    winners_raw = (vals["b_w"]["a_w"]["value"] or "1").strip()
    emoji = (
        (vals.get("b_e", {}).get("a_e", {}).get("value", "tada") or "tada")
        .strip(": ")
        .lower()
    )
    meta = json.loads(view.get("private_metadata", "{}"))
    channel_id = meta.get("channel_id")
    seconds = parse_duration(dur)
    try:
        winners = max(1, min(50, int(winners_raw)))
    except:
        winners = 1
    user_id = body["user"]["id"]
    if seconds <= 0 or not title or not channel_id:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="Provide a valid title and duration like `1d`, `2h30m`, or `45m`.",
        )
        return
    create_and_post(client, user_id, channel_id, title, seconds, emoji, winners)


def create_and_post(client, user_id, channel_id, title, seconds, emoji, winners):
    end_ts = int(time.time()) + int(seconds)
    header = f":{emoji}: *GIVEAWAY:* {title}"
    instructions = f"React with :{emoji}: to enter. Entries close automatically. Winners: {winners}"
    res = client.chat_postMessage(
        channel=channel_id,
        text=f"{header}\nEnds {slack_date(end_ts)}\n{instructions}",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Ends {slack_date(end_ts)} • Winners: *{winners}*",
                    }
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": instructions}},
        ],
    )
    ts = res["ts"]
    try:
        client.reactions_add(channel=channel_id, timestamp=ts, name=emoji)
    except Exception:
        pass
    db_execute(
        "INSERT INTO giveaways(channel_id, message_ts, emoji, creator_user_id, title, end_ts, winners_count, status) VALUES(%s,%s,%s,%s,%s,%s,%s,'open')",
        (channel_id, ts, emoji, user_id, title, end_ts, winners),
    )
    client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f"✅ Giveaway started: <{client.chat_getPermalink(channel=channel_id, message_ts=ts)['permalink']}>",
    )


def start_scheduler():
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()


if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.start(port=int(os.environ.get("PORT", 3000)))
