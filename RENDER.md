# 24/7 on Render (correct host)

Do not use Vercel. Vercel functions die after 10–60s. This bot needs a process that stays up, plus ffmpeg and disk.

## One-time setup

1. Open https://dashboard.render.com/select-repo?type=worker
2. Connect GitHub repo `Rahmowin-1st/Veltrix-Downloader`
3. Type: **Background Worker**
4. Runtime: **Docker**
5. Instance: Starter (free web service sleeps; worker needs a paid starter or any always-on plan)
6. Environment variable:
   - `BOT_TOKEN` = your BotFather token
7. Create Worker → wait until live

Blueprint URL (same repo):
https://render.com/deploy?repo=https://github.com/Rahmowin-1st/Veltrix-Downloader

After deploy, open @Veltrix_Downloader_bot and send /start.
