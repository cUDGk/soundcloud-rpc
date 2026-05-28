# -*- coding: utf-8 -*-
"""
SoundCloud -> Discord Rich Presence ブリッジ
Windows のメディアセッション API (GSMTC) から再生情報を取り、
Discord RPC に SoundCloud で聴いている曲として表示する常駐スクリプト。
"""
import asyncio
import time
import traceback

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.media import MediaPlaybackType
from pypresence import Presence

# ============ 設定 ============
# Discord Developer Portal で作った Application ID をここに入れる
CLIENT_ID = "YOUR_DISCORD_APP_ID_HERE"

# True にすると、SC_APP_AUMID が空のときは「ブラウザ AUMID のセッションだけ」を採用する。
# False なら、ブラウザでなくてもアーティスト名や除外ワードでゆるく判定する。
STRICT_MODE = True

# 特定アプリ(例: SoundCloud デスクトップ版 / PWA) の AUMID を直接ピン留めしたい時だけ書く。
# 空文字なら使わない (ブラウザ判定にフォールバック)。
SC_APP_AUMID = ""

# ポーリング間隔 (秒)
POLL_INTERVAL = 5

# Discord Developer Portal -> Rich Presence -> Art Assets に登録した画像名
LARGE_IMAGE_KEY = "soundcloud"
# 大きいアイコンにマウスを乗せた時のテキスト
LARGE_IMAGE_TEXT = "SoundCloud"

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
    総尺が取れなかったり 0 なら end は None にして経過カウンターだけにフォールバック。"""
    now = time.time()

    try:
        position_sec = info["position"].total_seconds()
        end_sec = info["end_time"].total_seconds()
    except Exception:
        return int(now), None

    if end_sec <= 0 or end_sec <= position_sec:
        # 総尺不明 -> 経過カウンターのみ
        return int(now - position_sec), None

    start_epoch = int(now - position_sec)
    end_epoch = int(start_epoch + end_sec)
    return start_epoch, end_epoch


def update_presence(rpc, info, last_key):
    """Discord RPC を更新。曲が変わった時だけ標準出力にログを出す。"""
    title = info["title"]
    artist = info["artist"]
    key = f"{title}\0{artist}"

    start_epoch, end_epoch = build_timestamps(info)

    # Discord 側の上限は details / state ともに 128 文字
    kwargs = {
        "details": (title or "Unknown")[:128],
        "state": (f"by {artist}")[:128],
        "large_image": LARGE_IMAGE_KEY,
        "large_text": LARGE_IMAGE_TEXT,
        "start": start_epoch,
    }
    if end_epoch is not None:
        kwargs["end"] = end_epoch

    rpc.update(**kwargs)

    if key != last_key:
        print(f"[♪] {title} - {artist}  (aumid={info['aumid']}, type={info['playback_type']})")
    return key


async def main_loop():
    rpc = Presence(CLIENT_ID)
    rpc.connect()
    print(f"[+] Discord RPC connected. CLIENT_ID={CLIENT_ID}")

    last_key = ""
    last_active = False

    try:
        while True:
            try:
                info = await pick_soundcloud_session()
                if info is None:
                    # 該当セッション無し -> プレゼンスを消す (前回 active だった時だけログ)
                    if last_active:
                        rpc.clear()
                        print("[-] No SoundCloud session. Presence cleared.")
                        last_active = False
                        last_key = ""
                else:
                    last_key = update_presence(rpc, info, last_key)
                    last_active = True
            except Exception:
                # 1 周分の例外は握り潰してループ継続 (RPC 再接続失敗等で落とさない)
                traceback.print_exc()

            await asyncio.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        try:
            rpc.clear()
        except Exception:
            pass
        try:
            rpc.close()
        except Exception:
            pass
        print("[x] Shutdown clean.")


if __name__ == "__main__":
    asyncio.run(main_loop())
