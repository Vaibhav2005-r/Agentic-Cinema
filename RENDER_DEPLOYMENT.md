# 🚀 Deploy to Render.com

> **SLO Watchdog is ready to deploy!** Get a live, shareable link in 5 minutes.

## Step-by-Step: Deploy to Render

### 1️⃣ Visit Render Dashboard

Go to [render.com](https://render.com) and sign in/sign up with GitHub

### 2️⃣ Create New Web Service

1. Click **"New +"** → **"Web Service"**
2. Select **"Build and deploy from a Git repository"**
3. Connect your GitHub account (if not already connected)
4. Select this repository: `Vaibhav2005-r/Agentic-Cinema`

### 3️⃣ Configure Service

Fill in these settings:

| Setting | Value |
|---------|-------|
| **Name** | `slo-watchdog` |
| **Environment** | `Docker` |
| **Region** | *(Choose closest to you)* |
| **Branch** | `main` |
| **Dockerfile Path** | `Dockerfile` |
| **Plan** | `Free` ($0/month) |

Click **"Create Web Service"**

### 4️⃣ Add Environment Variables

While the service is building, set up your secrets:

1. In the Render dashboard, go to **Environment**
2. Add these variables:

```
GRAFANA_URL=https://yourstack.grafana.net
GRAFANA_SA_TOKEN=glsa_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_API_KEY=your-gemini-api-key
```

⚠️ **Important:** 
- `GRAFANA_URL` - no trailing slash
- Get `GRAFANA_SA_TOKEN` from Grafana → Administration → Service accounts
- Get `GOOGLE_API_KEY` from [ai.google.dev](https://ai.google.dev)

### 5️⃣ Deploy

Render will automatically build and deploy your app. Wait for:
- ✅ Docker image build
- ✅ Service deployment
- ✅ Health checks to pass

### 6️⃣ Get Your Live URL

Once deployed, Render shows your public URL:

```
https://slo-watchdog-xxxx.onrender.com
```

**This is your shareable demo link!** 🎉

---

## ✅ Verify Deployment

Test your live console:

```bash
# 1. Check if console is running
curl https://slo-watchdog-xxxx.onrender.com

# 2. Check API health
curl https://slo-watchdog-xxxx.onrender.com/api/health

# Expected response:
# {
#   "ok": true,
#   "grafana_url": "https://yourstack.grafana.net",
#   "tools_advertised": 44,
#   "required_present": 11,
#   "required_total": 11
# }

# 3. Access console
open https://slo-watchdog-xxxx.onrender.com
```

---

## 🎯 What Judges Will See

When you share your link, judges will see:

1. **Web Console** - Interactive dashboard to run sweeps
2. **API Endpoints:**
   - `GET /` - Main web UI
   - `GET /api/health` - Service status
   - `GET /api/state` - Current sweep state
   - `POST /api/sweep` - Run a detection sweep
   - `GET /api/scenarios` - Available chaos scenarios
   - `POST /api/scenarios/{name}` - Toggle failure injection

---

## 📊 Monitor Your Deployment

### View Logs
1. Render dashboard → Your service
2. Click **"Logs"** tab
3. See real-time console output

### Check Metrics
1. Click **"Metrics"** tab
2. Monitor CPU, memory, network usage

### Update Code
- Push to `main` branch
- Render auto-deploys (watch in Deployments tab)

---

## 🛠️ Troubleshooting

### "Build Failed"
Check the build logs in Render dashboard. Common issues:
- Missing dependencies (check `pyproject.toml`)
- Docker syntax error (check `Dockerfile`)

### "Service not healthy"
Run this to debug:
```bash
curl https://your-url/api/health -v
```

Common issues:
- `GRAFANA_URL` is wrong or has trailing slash
- `GRAFANA_SA_TOKEN` is invalid
- `GOOGLE_API_KEY` is invalid

### "Port already in use"
Render auto-assigns ports. The Dockerfile uses environment variable `$PORT` - this is handled automatically.

---

## 📝 Add to Your Hackathon Submission

Include this in your project description:

```markdown
## 🎬 Live Demo

**[SLO Watchdog Console](https://slo-watchdog-xxxx.onrender.com)**

Deployed and running on Render.com. The autonomous agent finds viewer-facing 
failures in streaming pipelines that conventional alerting misses.

### Try It Out
1. Visit the link above
2. Set your Grafana URL and credentials
3. Click "Run Sweep" to see SLO burn rate detection in action
```

---

## 🚀 Next Steps

1. **Deploy** to Render using steps above
2. **Copy your live URL** from Render dashboard
3. **Test everything works** (visit `/api/health`)
4. **Share with judges** at your hackathon
5. **Watch the logs** as judges interact with your app

---

## Free Tier Info

✅ **Render Free Tier Includes:**
- Up to 750 hours/month of web service runtime
- Auto-deploys on git push
- SSL certificate (HTTPS)
- 100GB bandwidth/month
- PostgreSQL/Redis available (if needed)

⏱️ **Limitation:** Service spins down after 15 min of inactivity (spins up again on request)

---

## Alternative: Docker Compose (Local Testing)

Test locally before deploying:

```bash
# 1. Create .env file
cp .env.example .env
# Edit with your credentials

# 2. Run with Docker Compose
docker-compose up

# 3. Access at http://localhost:8080
```

---

**Questions?** Check:
- [Render Docs](https://render.com/docs)
- [SLO Watchdog README](https://github.com/Vaibhav2005-r/Agentic-Cinema)
- [Grafana Cloud Setup](https://grafana.com/products/cloud/)

**Happy coding! 🎉**
