# Free Fire Hub Bot

Built by **Boltix Labs**. Telegram bot (aiogram 3) with a Free Fire menu, UID lookup, weapon match, HUD card generator and AI advisor (Agnes).

Channels: [@Boltix_labs](https://t.me/Boltix_labs) · [@Hoshigami_anime](https://t.me/Hoshigami_anime)

## Mechanism
Every button opens a **new message** with its own square image, caption and buttons:
- **UID Lookup**: asks for a UID, then sends the player profile.
- **Weapon Match**: pick Rusher, Sniper, Assault, Support, Marksman or Flanker. The AI returns the best weapon match.
- **Top Up**: "This event hasn't been added yet", with Back and Continue.
- **Generate HUD**: asks for a UID, then sends a HUD card image built from the profile.
- **AI Advisor**: answers typed questions until you tap Back.
- **Sensitivity**: pick Headshot, Drag, Sniper, UMP or Desert Eagle, then type your device name. The AI corrects misspellings and estimates the settings, and the bot sends a slider card with your device and category.
- **Anime Hub**: link to the Hoshigami channel.

## Files (all at repo root)
`bot.py` (the whole bot, one file), `render.yaml`, `requirements.txt`, `.env.example`, and images `menu.jpg`, `uid.jpg`, `topup.jpg`, `weapon.jpg`, `hud.jpg`, `ai.jpg`, `anime.jpg`, `sensi.jpg`.

## Deploy on Render
1. Push all files to the root of your GitHub repo.
2. Render → **New → Blueprint** → pick the repo.
3. Fill in `BOT_TOKEN` and `GEMINI_API_KEY`. `AGNES_API_KEY` is an optional fallback. Add other keys only if you use them.
4. Make the bot an **admin of both channels** so the join check works.
5. Send `/start` to the bot.

## Notes
- The UID lookup uses a third-party host (free-ff-api on Render's free tier). It can be slow or offline. You can deploy the same repo yourself and point `FF_API_URL_TEMPLATE` at it.
- Check any lookup provider's terms before using it.
- The two databases are created and linked, but the bot doesn't read or write them yet.
- Render's free Postgres expires after 30 days unless upgraded.
- Sessions are kept in memory, so a restart clears any pending UID or AI prompt.

- Sensitivity values are AI estimates. Without `SERPER_API_KEY` the AI answers from its own knowledge, which can be out of date. Check the numbers in game.

- Gemini is the main AI (`GEMINI_MODEL=gemini-3.5-flash-lite`). If Render's logs show a 404 for the model, change `GEMINI_MODEL` to a model name your key supports.
