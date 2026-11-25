# eTL 비서 (Claude Code + Telegram)

서울대 eTL(Canvas), 구글 캘린더, Gmail, Notion, 날씨를 묶어 **텔레그램으로 말을 거는 개인 비서**입니다.
리눅스 PC에서 Claude Code 세션을 tmux로 계속 띄워 두고, 텔레그램 봇으로 대화합니다.

- 매일 08:00 아침 브리핑: 오늘 일정 · 마감 · 공지 · 날씨(우산 여부) · 메일 요약 + "언제 뭘 하면 좋을지" 리뷰
- 21:00 저녁 체크: 내일 날씨, 30시간 안에 마감인 미제출 과제
- 수시로: "HW3 제출 제대로 됐어?", "내일 우산 필요해?", "이번 주 마감 뭐 있어?"
- 제출 확인: 제출한 파일을 내려받아 크기·확장자·지각 여부를 확인하고, 내 파일과 바이트 단위로 비교

eTL 접근은 **읽기 전용(GET)** 이고, 토큰은 Claude가 읽을 수 없는 설정 파일에만 저장됩니다.

## 구성

```
assistant/            Claude Code 프로젝트 (여기서 claude가 실행됨)
  CLAUDE.md           비서의 역할·도구·브리핑 형식·규칙
  .claude/settings.json   허용 명령 목록 (dontAsk 모드: 목록 밖 명령은 자동 거부)
  .claude/commands/   /briefing, /inbox (cron이 호출)
  .mcp.json           Notion MCP
  bin/etl             eTL 조회 (etl-digest/etl_digest.py 래퍼)
  bin/cal, cal.py     구글 캘린더 iCal 읽기 (커넥터가 없을 때의 대안)
  bin/weather, weather.py  시간별 날씨 + 우산 판단 (MET Norway, API 키 불필요)
  start.sh            tmux 안에서 claude를 계속 실행
  trigger.sh          cron → 실행 중인 세션에 /briefing 입력
  notes/              비서의 메모 (개인 정보, git에 올라가지 않음)
etl-digest/           eTL 클라이언트 (단독으로 쓰면 매일 메일 요약도 가능)
setup/                systemd 유닛, crontab 예시, 캘린더 설정 예시
```

## 준비물

- 리눅스 PC (Debian/Ubuntu 기준, 항상 켜져 있는 기기), `python3` 3.9+, `tmux`
- [Claude Code](https://code.claude.com) 설치 및 로그인
- 텔레그램 계정, 서울대 eTL 계정

## 설치

### 1. 받기

```sh
sudo apt install -y git tmux python3
git clone https://github.com/<당신의 계정>/etl-assistant.git ~/etl-assistant
```

다른 위치에 받았다면 아래 `~/etl-assistant` 경로와 `setup/` 파일들의 경로를 바꿔 주세요.

### 2. eTL 토큰

1. https://myetl.snu.ac.kr/profile/settings → **새 액세스 토큰** 발급
2. 설정 파일 만들기:
   ```sh
   mkdir -p ~/.config/etl-digest && chmod 700 ~/.config/etl-digest
   cp ~/etl-assistant/etl-digest/config.example.json ~/.config/etl-digest/config.json
   chmod 600 ~/.config/etl-digest/config.json
   ```
3. `config.json`의 `etl_token`에 토큰을 붙여넣기. (메일 항목은 단독 메일 요약을 쓸 때만 필요)
4. 확인: `~/etl-assistant/assistant/bin/etl --check` → `eTL OK: <이름>`

### 3. 텔레그램 봇

1. 텔레그램에서 **@BotFather** → `/newbot` → 봇 토큰 받기
2. Claude Code에 텔레그램 플러그인 설치 (claude 안에서 `/plugin` → `telegram@claude-plugins-official`)
3. 토큰 저장 (이 비서 전용 상태 폴더):
   ```sh
   mkdir -p ~/.claude/channels/telegram-etl
   echo 'TELEGRAM_BOT_TOKEN=123456:ABC...' > ~/.claude/channels/telegram-etl/.env
   chmod 600 ~/.claude/channels/telegram-etl/.env
   ```
4. 5단계에서 세션을 띄운 뒤 봇에게 DM을 보내 페어링하고, **터미널에서** `/telegram:access`로 본인만 허용하세요.
   허용 목록에 들어간 본인의 숫자 chat_id를 다음 단계에서 씁니다.

### 4. 내 정보 채우기

`assistant/` 안의 자리표시자를 바꿉니다.

```sh
cd ~/etl-assistant/assistant
sed -i 's/YOUR_TELEGRAM_CHAT_ID/123456789/g' CLAUDE.md .claude/commands/*.md
sed -i 's/YOUR_SNU_MAIL_LABEL/서울대 메일 라벨 이름/g' CLAUDE.md
```

- `YOUR_SNU_MAIL_LABEL`: 서울대 단체 메일이 받은편지함을 건너뛰고 들어가는 Gmail 라벨. 없으면 CLAUDE.md의 그 문장을 지워도 됩니다.
- 날씨 기본 위치는 서울대 관악캠퍼스입니다. 바꾸려면 `bin/weather.py`의 `SNU = (위도, 경도)`를 수정하세요.
  집 날씨도 같이 받고 싶으면 비서에게 "집은 OO동이야, 브리핑에 집 날씨도 넣어줘"라고 말하면 메모에 저장해 둡니다.

### 5. 실행 (systemd + tmux)

```sh
mkdir -p ~/.config/systemd/user
cp ~/etl-assistant/setup/etl-assistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now etl-assistant
sudo loginctl enable-linger "$USER"     # 로그아웃해도 계속 실행
tmux attach -t etl-assistant
```

처음 한 번만 tmux 안에서:
- "trust this folder" 수락
- `/mcp` → notion → 인증 (브라우저에서 공유할 페이지/DB 선택)
- 텔레그램 페어링 (3-4단계)
- **Ctrl-b, d** 로 빠져나오기 (`/exit` 입력하지 말 것)

### 6. 스케줄 (cron)

```sh
crontab -e
```

`setup/crontab.example` 내용을 붙여넣고 `/home/YOU`를 본인 홈 경로로 바꿉니다.
`XDG_RUNTIME_DIR`의 `1000`은 `id -u` 값으로 바꾸세요.

### 7. (선택) 구글 캘린더 · Gmail

- **claude.ai 커넥터**(Google Calendar, Gmail)를 claude.ai 설정 → 커넥터에서 연결해 두면 세션에서 바로 쓸 수 있습니다. Gmail은 읽기 도구만 허용되어 있습니다.
- 커넥터가 없으면 iCal 대안:
  ```sh
  mkdir -p ~/.config/etl-assistant && chmod 700 ~/.config/etl-assistant
  cp ~/etl-assistant/setup/calendars.example.json ~/.config/etl-assistant/calendars.json
  chmod 600 ~/.config/etl-assistant/calendars.json
  ```
  구글 캘린더 → 설정 → 해당 캘린더 → 캘린더 통합 → **iCal 형식의 비밀 주소**를 붙여넣고
  `~/etl-assistant/assistant/bin/cal --days 7` 으로 확인.

### 8. 테스트

텔레그램 봇에게: "오늘 일정 알려줘", "내일 우산 필요해?"

## 동작 확인 / 문제 해결

| 증상 | 확인 |
| --- | --- |
| 브리핑이 안 옴 | `assistant/trigger.log`, `tmux attach -t etl-assistant` |
| "토큰 만료" | eTL에서 새 토큰 발급 → `~/.config/etl-digest/config.json` 수정 |
| 새 명령을 비서가 못 씀 | `assistant/.claude/settings.json`의 `allow`에 추가 후 `systemctl --user restart etl-assistant` |
| 날씨 실패 | `assistant/bin/weather` 직접 실행 (api.met.no 접속 필요) |

## 보안 메모

- 비서는 `dontAsk` 모드로 돌아가며 `settings.json`에 허용된 명령만 실행합니다. 토큰 파일 읽기, `.claude/` 수정, 메일 발송·삭제, 웹 fetch는 거부되어 있습니다.
- 메일·다운로드 파일 내용은 신뢰하지 않는 데이터로 취급하도록 CLAUDE.md에 규칙이 있습니다.
- `notes/`, `downloads/`, 로그, 각종 설정 파일은 `.gitignore`로 제외되어 있습니다. 포크해서 쓸 때 개인 정보를 커밋하지 않도록 주의하세요.

## 크레딧

eTL 클라이언트는 [hyukjunlim/claude-pet](https://github.com/hyukjunlim/claude-pet)의 읽기 전용 eTL 클라이언트를 파이썬 표준 라이브러리로 옮긴 것입니다.
