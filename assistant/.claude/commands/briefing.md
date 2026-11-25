---
description: Send the scheduled briefing to the user on Telegram
argument-hint: "[morning|evening]"
---

Scheduled trigger (no one typed this in Telegram): send the **$ARGUMENTS** briefing to
chat_id YOUR_TELEGRAM_CHAT_ID with the telegram `reply` tool.

1. Read `notes/memory.md` if it exists.
2. Run `date`, `bin/etl`, `bin/cal` and `bin/weather` (morning: today; evening: tomorrow via `--from` / `--tomorrow`).
   Check Notion for open tasks due today/tomorrow if you know which database holds them
   (from memory.md); otherwise search Notion briefly for a task database and note it in memory.md.
3. Morning: follow the briefing format in CLAUDE.md — today's schedule, deadlines, Notion
   tasks, new announcements, and a concrete review of when to do what.
   Evening: tomorrow's weather line, then only things due in the next ~30 hours that aren't submitted, plus tomorrow's first
   event. If nothing is due, send one short line (e.g. "내일까지 마감 없음 ✅ 첫 일정 10:30 자료구조").
4. Send it as one new message (not an edit). Don't print it to the terminal.
