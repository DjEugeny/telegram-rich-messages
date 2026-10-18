window.STATE =
{
  "slug": "telegram-scheduling",
  "dir": "2026-10-18-telegram-scheduling--wip",
  "title": "Отложенные Rich Messages силами Telegram",
  "mode": "full",
  "depth": "normal",
  "polish": null,
  "tier": null,
  "briefFile": "2026-10-18-brief.md",
  "memoryFile": "AGENTS.md",
  "skillDir": "/Users/evgenyzach/.agents/skills/autopilot",
  "startedAt": "2026-10-18T09:04:55+07:00",
  "updatedAt": "2026-10-18T09:29:20+07:00",
  "stages": [
    {
      "id": "preflight",
      "status": "done",
      "startedAt": "2026-10-18T09:04:55+07:00"
    },
    {
      "id": "manifest",
      "status": "done",
      "startedAt": "2026-10-18T09:04:55+07:00"
    },
    {
      "id": "briefing",
      "status": "skipped",
      "note": "полный автомат — самобрифинг"
    },
    {
      "id": "spec",
      "status": "done",
      "startedAt": "2026-10-18T09:17:00+07:00"
    },
    {
      "id": "plan",
      "status": "done",
      "startedAt": "2026-10-18T09:28:56+07:00"
    },
    {
      "id": "build",
      "status": "active",
      "startedAt": "2026-10-18T09:28:56+07:00"
    },
    {
      "id": "review",
      "status": "pending"
    },
    {
      "id": "final",
      "status": "pending"
    }
  ],
  "requirements": {
    "total": 30,
    "done": 0,
    "inTicket": 0,
    "inSpec": 24,
    "placeholder": 4,
    "deferred": 2,
    "dropped": 0
  },
  "tickets": [
    {
      "id": "01",
      "title": "Устойчивый experimental-адаптер расписания",
      "requirements": [
        "R01",
        "R02",
        "R03",
        "R04",
        "R05",
        "R06",
        "R07",
        "R08",
        "R09",
        "R10",
        "R12",
        "R13",
        "R14",
        "R18",
        "R19",
        "R20",
        "R21",
        "R22",
        "R23"
      ],
      "blockedBy": [],
      "wave": 1,
      "zone": [
        "scheduling.py",
        "delivery.py",
        "tests/test_scheduling.py"
      ],
      "status": "active",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-10-18T09:29:20+07:00"
    },
    {
      "id": "02",
      "title": "Агентные команды расписания",
      "requirements": [
        "R03",
        "R04",
        "R06",
        "R07",
        "R10",
        "R11",
        "R12",
        "R13",
        "R16",
        "R17",
        "R21",
        "R23"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "tool.py",
        "tests/test_schedule_cli.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "03",
      "title": "Состояния и даты расписания в превью",
      "requirements": [
        "R09",
        "R11",
        "R14",
        "R15",
        "R22",
        "R23"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "dashboard.py",
        "web/",
        "tests/test_dashboard.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "04",
      "title": "Инструкции новичку и live-приёмка",
      "requirements": [
        "R01",
        "R02",
        "R03",
        "R04",
        "R10",
        "R12",
        "R13",
        "R16",
        "R17",
        "R24",
        "R25",
        "R26",
        "R27",
        "R28",
        "R29",
        "R30"
      ],
      "blockedBy": [
        "01",
        "02",
        "03"
      ],
      "wave": 3,
      "zone": [
        "README.md",
        "ONBOARDING.md",
        "HANDOFF.md",
        "API-NOTES.md",
        "docs/"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    }
  ],
  "singlePass": null,
  "tests": null,
  "debt": {
    "placeholders": [],
    "assumptions": [],
    "emptyEnv": []
  },
  "additions": [],
  "coverage": {
    "findings": 14,
    "resolution": "6 missing -> §9/R29-R30 deferred; 5 half-covered -> §1/§3/§7 strengthened; rest = in-spec detailing"
  },
  "concerns": [
    "Live Telegram и GitHub требуют отдельного разрешения по HANDOFF.md.",
    "Коммиты не разрешены пользователем; существующий untracked проект сохраняется."
  ],
  "reviewers": {
    "manifestSpec": null,
    "craft": null
  },
  "blind": null,
  "finishedAt": null
}