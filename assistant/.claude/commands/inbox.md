---
description: Periodic check for new unread inbox mail; message the user only if there is some
---

Scheduled trigger (no one typed this in Telegram).

1. Read `notes/inbox-seen.json` (missing → treat as empty).
2. `search_threads` with `in:inbox is:unread newer_than:2d`, view `THREAD_VIEW_SUMMARY`.
3. Keep only messages whose id is not in `reported_ids`.
4. If none: send nothing. Just update `last_check` and stop.
5. Otherwise send ONE short Telegram message to chat_id YOUR_TELEGRAM_CHAT_ID:
   ```
   📬 새 메일 N통 (읽지 않음)
   • 보낸이 — 제목 : 한 줄 요약 [⚠️ 답장 필요 / ⏰ 마감 MM/DD / 🔐 보안 알림 등]
   ```
   Open a thread with `get_thread` only if the snippet isn't enough to say what it wants.
   Security notices (new sign-in, token created): say whether it looks like the user's own
   activity based on time/device, but don't guess beyond that.
6. Add the reported ids to `reported_ids` (keep the newest ~300), set `last_check`, save.
