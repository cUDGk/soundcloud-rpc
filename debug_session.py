# -*- coding: utf-8 -*-
"""
全メディアセッションを 1 回だけダンプして中身を確認するデバッグスクリプト。
SoundCloud を再生した状態でこれを叩き、AUMID / playback_type / artist が
期待通りに取れるかを目視で確認するために使う。
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)


async def main():
    mgr = await SessionManager.request_async()
    sessions = mgr.get_sessions()
    print(f"sessions found: {len(sessions)}")

    for i, s in enumerate(sessions):
        print(f"\n--- session #{i} ---")
        try:
            aumid = s.source_app_user_model_id
            print(f"AUMID            : {aumid}")

            pb = s.get_playback_info()
            print(f"playback_status  : {pb.playback_status}")

            props = await s.try_get_media_properties_async()
            print(f"title            : {props.title}")
            print(f"artist           : {props.artist}")
            print(f"album_title      : {props.album_title}")
            print(f"album_artist     : {props.album_artist}")
            print(f"playback_type    : {props.playback_type}  ({type(props.playback_type).__name__})")
            print(f"genres           : {list(props.genres) if props.genres else []}")

            tl = s.get_timeline_properties()
            print(f"position         : {tl.position}")
            print(f"end_time         : {tl.end_time}")
            print(f"start_time       : {tl.start_time}")
            print(f"last_updated_time: {tl.last_updated_time}")
        except Exception as e:
            print(f"error: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
