# Deployment Guide (Phase 4)

# Deployment Guide (Phase 4)

**Status: paused.** Deployed successfully to Render + Vercel, but
yfinance gets rate-limited on Render's shared IPs (`YFRateLimitError:
Too Many Requests` on every request) — confirmed in production, not
just a risk. See README's Known Limitations for the full writeup. The
app runs correctly on localhost; this guide is kept for when the data
layer is swapped to a licensed API (Finnhub/Alpha Vantage) for the
hosted path, or if intermittent yfinance failures become acceptable
for demo purposes.

Backend -> Render (free tier). Frontend -> Vercel (free tier).

**Known limitation of this setup**: Render's free tier has no persistent
disk. `db/prices.db` (price cache) and `db/sessions.db` (chat history)
reset on every restart/redeploy, and the free service spins down after
~15 min idle (next request takes ~1 min to wake it up). Fine for
personal/demo use. If this becomes annoying, upgrade to a paid Render
instance type with a persistent disk add-on.

---

## Part 1: Push to GitHub (skip if already done)

```bash
git add .
git commit -m "Phase 4: deployment config"
git push
```

## Part 2: Deploy the backend to Render

1. Go to https://render.com, sign up/log in (GitHub login is easiest)
2. Dashboard -> **New** -> **Blueprint**
3. Connect your GitHub account, select this repo
4. Render reads `render.yaml` automatically and shows you the planned service - confirm
5. Before it finishes, go to the service's **Environment** tab and add:
   - `ANTHROPIC_API_KEY` = your real key (never commit this - `render.yaml` deliberately leaves it blank with `sync: false`)
6. Deploy. Wait for the build to finish (few minutes first time)
7. Copy your live backend URL, e.g. `https://stock-agent-backend-xxxx.onrender.com`
8. Test it directly:
   ```bash
   curl https://stock-agent-backend-xxxx.onrender.com/health
   ```
   Should return `{"status": "ok"}`. If it times out, the free service may be spinning up - wait ~1 min and retry.

## Part 3: Deploy the frontend to Vercel

1. Go to https://vercel.com, sign up/log in (GitHub login is easiest)
2. **Add New** -> **Project** -> import this repo
3. Set the **Root Directory** to `frontend` (important - the repo root is not the frontend app)
4. Vercel should auto-detect Vite via `vercel.json`; confirm build command is `npm run build`, output is `dist`
5. Add an environment variable before deploying:
   - `VITE_API_URL` = your Render backend URL from Part 2 (e.g. `https://stock-agent-backend-xxxx.onrender.com`)
6. Deploy. Copy your live frontend URL, e.g. `https://stock-agent-xxxx.vercel.app`

## Part 4: Connect them (CORS)

Your backend only accepts requests from known origins. Now that you have a real frontend URL:

1. Back in Render's dashboard, go to your backend service's **Environment** tab
2. Add: `FRONTEND_URL` = your Vercel URL from Part 3 (e.g. `https://stock-agent-xxxx.vercel.app`)
3. Render will auto-redeploy with this new setting

## Part 5: Test the real thing

Open your Vercel URL in a browser (any device, not just your laptop) and ask a question. First request may be slow (~1 min) if the backend was asleep - that's the free-tier spin-down, not a bug.

---

## Troubleshooting

- **CORS error in browser console**: `FRONTEND_URL` on Render doesn't exactly match your Vercel URL (check for trailing slash, http vs https)
- **"Can't reach the agent backend"**: `VITE_API_URL` on Vercel is wrong, or the Render service is still spinning up - wait and retry
- **Answers are correct but slow on first request**: expected free-tier cold start, not a bug
- **Chat history disappears after a while**: expected - no persistent disk on the free tier (see limitation above)