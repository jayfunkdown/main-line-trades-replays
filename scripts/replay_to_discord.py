#!/usr/bin/env python3
import json, os, sys, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"].strip()
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"].strip()
TITLE_PREFIX = os.getenv("TITLE_PREFIX", "🔴 Live trading Crypto Futures Forex Stocks - NY Open").strip()
SEND_TEST = os.getenv("SEND_TEST", "false").lower() == "true"
STATE_PATH = Path("data/posted_ids.json")
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
NS = {"atom":"http://www.w3.org/2005/Atom","yt":"http://www.youtube.com/xml/schemas/2015"}

def post(content):
    data = json.dumps({"username":"Main Line Trades Replays","content":content,"allowed_mentions":{"parse":[]}}).encode()
    req = urllib.request.Request(WEBHOOK_URL,data=data,headers={"Content-Type":"application/json","User-Agent":"MLT-ReplayBot/1.0"},method="POST")
    with urllib.request.urlopen(req,timeout=30) as r:
        if r.status not in (200,204):
            raise RuntimeError(f"Discord HTTP {r.status}")

def fetch():
    req = urllib.request.Request(FEED_URL,headers={"User-Agent":"MLT-ReplayBot/1.0"})
    with urllib.request.urlopen(req,timeout=30) as r:
        root = ET.fromstring(r.read())
    out=[]
    for e in root.findall("atom:entry",NS):
        title=(e.findtext("atom:title","",NS) or "").strip()
        vid=(e.findtext("yt:videoId","",NS) or "").strip()
        pub=(e.findtext("atom:published","",NS) or "").strip()
        url=""
        for n in e.findall("atom:link",NS):
            if n.attrib.get("rel")=="alternate":
                url=n.attrib.get("href","")
                break
        if title.startswith(TITLE_PREFIX) and vid and url:
            out.append({"id":vid,"title":title,"url":url,"published":pub})
    return sorted(out,key=lambda x:x["published"])

def load_ids():
    if not STATE_PATH.exists():
        return []
    try:
        x=json.loads(STATE_PATH.read_text())
        return [str(i) for i in x] if isinstance(x,list) else []
    except Exception:
        return []

def save_ids(ids):
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    STATE_PATH.write_text(json.dumps(ids[-100:],indent=2)+"\n")

def main():
    if SEND_TEST:
        post("✅ **YouTube replay automation test successful.**\nFuture matching live-stream replays will be posted in this channel.")
        print("Test message sent.")
        return
    videos=fetch()
    ids=load_ids()
    if not STATE_PATH.exists():
        save_ids([v["id"] for v in videos])
        print(f"Initialized with {len(videos)} matching video(s); nothing posted.")
        return
    seen=set(ids)
    new=[v for v in videos if v["id"] not in seen]
    for v in new:
        post(f"🎥 **New Live Stream Replay**\n\n**{v['title']}**\n{v['url']}\n\nMissed the New York Open? Watch the full replay here.")
        ids.append(v["id"])
        print("Posted:",v["title"])
    save_ids(ids)
    print(f"Finished. {len(new)} new replay(s) posted.")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:",e,file=sys.stderr)
        raise
