# -*- coding: utf-8 -*-
"""
SoundCloud -> Discord Rich Presence ブリッジ
Windows のメディアセッション API (GSMTC) から再生情報を取り、
Discord RPC に SoundCloud で聴いている曲として表示する常駐スクリプト。
"""
import asyncio
import re
import sys
import time
import traceback

import aiohttp  # pypresence の依存として入っている

# Windows のコンソールは既定で CP932。日本語タイトルのログを文字化けさせないため UTF-8 に揃える。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.media import MediaPlaybackType
# 同期版 Presence は内部で event loop を回すため asyncio.run() 配下で動かない。
# こちらは asyncio で動かすので非同期版を使う。
from pypresence import AioPresence

# ============ 設定 ============
# Discord Developer Portal で作った Application ID をここに入れる
CLIENT_ID = "1509490141079535656"

# True にすると、SC_APP_AUMID が空のときは「ブラウザ AUMID のセッションだけ」を採用する。
# False なら、ブラウザでなくてもアーティスト名や除外ワードでゆるく判定する。
STRICT_MODE = True

# 特定アプリ(例: SoundCloud デスクトップ版 / PWA) の AUMID を直接ピン留めしたい時だけ書く。
# 空文字なら使わない (ブラウザ判定にフォールバック)。
SC_APP_AUMID = ""

# ポーリング間隔 (秒)
POLL_INTERVAL = 5

# SoundCloud 検索でジャケットが見つからなかった時のフォールバック画像
# (Discord Developer Portal -> Rich Presence -> Art Assets に登録した画像名)
FALLBACK_IMAGE_KEY = "soundcloud"
# 大きいアイコンにマウスを乗せた時のテキスト
LARGE_IMAGE_TEXT = "SoundCloud"
# 曲リンクのボタンに表示するラベル (Discord の制限: 最大 32 文字)
BUTTON_LABEL = "SoundCloud で聴く"

# ============ フィルタ用 ============
# ブラウザ判定用のキーワード (AUMID を小文字化した中に含まれていれば通す)
BROWSER_KEYWORDS = ("chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi")

# タイトルに含まれていたら SoundCloud ではないとみなして弾く語
EXCLUDE_TITLE_WORDS = (
    "- YouTube",
    "Netflix",
    "Spotify",
    "Prime Video",
    "Hulu",
    "AbemaTV",
    "ニコニコ動画",
    "広告",
    "Advertisement",
)


def is_soundcloud_candidate(aumid: str, title: str, artist: str, playback_type) -> bool:
    """対象セッションが SoundCloud の再生かを多段でフィルタする。"""
    aumid_l = (aumid or "").lower()

    # 1. SC_APP_AUMID 指定時はそれに完全一致するセッションだけ採用
    if SC_APP_AUMID:
        if aumid_l != SC_APP_AUMID.lower():
            return False
    else:
        # ブラウザ判定: STRICT_MODE のときはブラウザ AUMID 限定、そうでなければ
        # 空 AUMID を弾く程度のゆるい判定
        if STRICT_MODE:
            if not any(k in aumid_l for k in BROWSER_KEYWORDS):
                return False
        else:
            if not aumid_l:
                return False

    # 2. アーティスト名が空ならほぼ SC ではない (SC はだいたい埋まる)
    if not artist or not artist.strip():
        return False

    # 3. タイトルに除外ワードを含むものを弾く (YouTube / Netflix / 広告 等)
    title_l = (title or "").lower()
    for w in EXCLUDE_TITLE_WORDS:
        if w.lower() in title_l:
            return False

    # 4. playback_type が Image など明らかに音楽でないものは弾く。
    #    SC は Video 報告のことがあるので Music / Video / Unknown はいずれも通す。
    try:
        if playback_type == MediaPlaybackType.IMAGE:
            return False
    except Exception:
        # 型が違う / 取得できない時は通す
        pass

    return True


# ============ SoundCloud 検索 (ジャケット & 曲リンク取得) ============
# SoundCloud は 2021 から公式 API の新規登録を止めているが、Web 版 SC は
# 自分自身が読み込む JS の中に client_id を埋めているので、それを抜けば
# api-v2.soundcloud.com を叩ける (古くからある常套手段)。

_SC_CLIENT_ID = None  # 一度取れたらメモリにキャッシュ
_SC_TRACK_CACHE: dict[str, dict | None] = {}  # (title, artist) -> 検索結果


async def get_sc_client_id(http: aiohttp.ClientSession) -> str | None:
    """SoundCloud の HTML から client_id を抜き出す。失敗時 None。"""
    global _SC_CLIENT_ID
    if _SC_CLIENT_ID:
        return _SC_CLIENT_ID

    try:
        async with http.get("https://soundcloud.com/", timeout=aiohttp.ClientTimeout(total=10)) as r:
            html = await r.text()
        # トップページが読み込んでいる JS をすべて拾う
        js_urls = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
        for js_url in js_urls:
            try:
                async with http.get(js_url, timeout=aiohttp.ClientTimeout(total=10)) as r2:
                    js = await r2.text()
            except Exception:
                continue
            m = re.search(r'client_id\s*[:=]\s*"([A-Za-z0-9]{32})"', js)
            if m:
                _SC_CLIENT_ID = m.group(1)
                print(f"[+] SoundCloud client_id を取得: {_SC_CLIENT_ID[:6]}...")
                return _SC_CLIENT_ID
    except Exception:
        traceback.print_exc()

    print("[!] SoundCloud client_id を取得できず。ジャケットとリンクはフォールバックします。")
    return None


def _hires_artwork(url: str | None) -> str | None:
    """SC のサムネ URL を 500x500 版に置き換える。'-large.jpg' -> '-t500x500.jpg'"""
    if not url:
        return None
    return url.replace("-large.", "-t500x500.")


async def search_sc_track(http: aiohttp.ClientSession, title: str, artist: str) -> dict | None:
    """タイトル + アーティストで SC を検索。アートワーク URL と曲ページ URL を返す。"""
    key = f"{title}\0{artist}"
    if key in _SC_TRACK_CACHE:
        return _SC_TRACK_CACHE[key]

    cid = await get_sc_client_id(http)
    if not cid:
        _SC_TRACK_CACHE[key] = None
        return None

    q = f"{title} {artist}".strip()
    params = {"q": q, "client_id": cid, "limit": 5}
    try:
        async with http.get(
            "https://api-v2.soundcloud.com/search/tracks",
            params=params,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            data = await r.json()
    except Exception:
        traceback.print_exc()
        _SC_TRACK_CACHE[key] = None
        return None

    hits = data.get("collection") or []
    if not hits:
        _SC_TRACK_CACHE[key] = None
        return None

    # 先頭ヒットを採用 (SC の検索は relevance ソート)
    top = hits[0]
    result = {
        "artwork_url": _hires_artwork(top.get("artwork_url")),
        "permalink_url": top.get("permalink_url"),
        "title": top.get("title"),
        "user_name": (top.get("user") or {}).get("username"),
    }
    _SC_TRACK_CACHE[key] = result
    return result


async def pick_soundcloud_session():
    """全セッションから SoundCloud の PLAYING を 1 つ返す。無ければ None。"""
    mgr = await SessionManager.request_async()
    sessions = mgr.get_sessions()

    for session in sessions:
        try:
            playback = session.get_playback_info()
            if playback.playback_status != PlaybackStatus.PLAYING:
                continue

            props = await session.try_get_media_properties_async()
            title = props.title or ""
            artist = props.artist or ""
            playback_type = props.playback_type
            aumid = session.source_app_user_model_id or ""

            if not is_soundcloud_candidate(aumid, title, artist, playback_type):
                continue

            timeline = session.get_timeline_properties()

            return {
                "title": title.strip(),
                "artist": artist.strip(),
                "aumid": aumid,
                "playback_type": playback_type,
                "position": timeline.position,           # timedelta
                "end_time": timeline.end_time,           # timedelta (0 のことあり)
                "last_updated": timeline.last_updated_time,  # datetime
            }
        except Exception:
            # 個別セッションでこけても次へ
            continue

    return None


def build_timestamps(info):
    """Discord RPC 用の start / end (epoch 秒) を作る。

    GSMTC の position はライブカウンタではなく last_updated_time 時点のスナップショット。
    なので「いま - position」で start を計算するとポーリング遅延の分ズレる。
    曲が始まった絶対時刻 = last_updated_time - position を基準にすると、
    Discord 側がいまの epoch との差分でバーを描いてくれるので常に正確になる。

    総尺が取れなかったり 0 なら end は None にして経過カウンターだけにフォールバック。"""
    now = time.time()

    try:
        position_sec = info["position"].total_seconds()
        end_sec = info["end_time"].total_seconds()
        # last_updated_time は tz aware の UTC datetime
        base_ts = info["last_updated"].timestamp()
    except Exception:
        return int(now), None

    # GSMTC が DateTime(0) (1601-01-01) を返す異常時や、未来時刻が混入した時は now にフォールバック
    if base_ts <= 0 or abs(now - base_ts) > 86400:
        base_ts = now

    start_epoch = int(base_ts - position_sec)

    if end_sec <= 0 or end_sec <= position_sec:
        # 総尺不明 -> 経過カウンターのみ
        return start_epoch, None

    end_epoch = int(start_epoch + end_sec)
    return start_epoch, end_epoch


async def update_presence(rpc, http, info, last_key):
    """Discord RPC を更新。曲が変わった時だけ SC を検索し、ログを出す。"""
    title = info["title"]
    artist = info["artist"]
    key = f"{title}\0{artist}"

    start_epoch, end_epoch = build_timestamps(info)

    # 曲が変わったタイミングで SC 検索を走らせる (同じ曲を毎ポーリングで叩かない)
    sc_meta: dict | None = None
    if key != last_key:
        sc_meta = await search_sc_track(http, title, artist)
    else:
        sc_meta = _SC_TRACK_CACHE.get(key)

    # アートワーク / リンクの組み立て
    if sc_meta and sc_meta.get("artwork_url"):
        large_image = sc_meta["artwork_url"]   # 外部 URL 直渡し (Discord は CDN プロキシ経由で取得)
    else:
        large_image = FALLBACK_IMAGE_KEY        # Art Asset 名にフォールバック

    # Discord 側の上限は details / state ともに 128 文字
    kwargs = {
        "details": (title or "Unknown")[:128],
        "state": (f"by {artist}")[:128],
        "large_image": large_image,
        "large_text": LARGE_IMAGE_TEXT[:128],
        "start": start_epoch,
    }
    if end_epoch is not None:
        kwargs["end"] = end_epoch

    if sc_meta and sc_meta.get("permalink_url"):
        # buttons: 自分自身からは見えないが、他人のプロフィールから見ると表示される (Discord 仕様)
        kwargs["buttons"] = [{"label": BUTTON_LABEL[:32], "url": sc_meta["permalink_url"]}]

    await rpc.update(**kwargs)

    if key != last_key:
        extra = ""
        if sc_meta:
            extra = f"  -> {sc_meta.get('permalink_url')}"
        elif _SC_CLIENT_ID:
            extra = "  (SC 検索ヒット無し、フォールバック)"
        print(f"[♪] {title} - {artist}  (aumid={info['aumid']}, type={info['playback_type']}){extra}")
    return key


async def main_loop():
    rpc = AioPresence(CLIENT_ID)
    await rpc.connect()
    print(f"[+] Discord RPC connected. CLIENT_ID={CLIENT_ID}")

    # SC 検索用の HTTP セッションをループ全体で 1 個共有 (接続再利用)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) sc-rpc/1.0"}
    ) as http:
        last_key = ""
        last_active = False

        try:
            while True:
                try:
                    info = await pick_soundcloud_session()
                    if info is None:
                        # 該当セッション無し -> プレゼンスを消す (前回 active だった時だけログ)
                        if last_active:
                            await rpc.clear()
                            print("[-] No SoundCloud session. Presence cleared.")
                            last_active = False
                            last_key = ""
                    else:
                        last_key = await update_presence(rpc, http, info, last_key)
                        last_active = True
                except Exception:
                    # 1 周分の例外は握り潰してループ継続 (RPC 再接続失敗等で落とさない)
                    traceback.print_exc()

                await asyncio.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n[!] Interrupted by user.")
        finally:
            try:
                await rpc.clear()
            except Exception:
                pass
            try:
                rpc.close()
            except Exception:
                pass
            print("[x] Shutdown clean.")


if __name__ == "__main__":
    # Windows 既定の ProactorEventLoop だと aiohttp の DNS resolver (aiodns) が動かない。
    # winsdk は COM 呼び出しなので Selector でも問題なく動くため、こちらに統一する。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main_loop())
