# ETP Explainer Deployment Guide

Production deployment guide for the ETP shipment lifecycle explainer on the rarko1 VM.

## Quick Reference: All chmod Commands

```bash
# Management script (make executable)
chmod +x ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/manage-explainer.sh

# Environment file (secure - contains passwords)
chmod 600 ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/.env

# Cache directory (writable)
sudo chown azureuser:azureuser /mnt/dqt/etp-explainer-cache
chmod 755 /mnt/dqt/etp-explainer-cache
```

## ⚠️ IMPORTANT: Use ~/cloudfiles/ Path Only

**Always use:**
```bash
~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations
```

**Never use the /mnt/ path - it's extremely slow (15+ seconds for git status):**
```bash
# DON'T USE THIS PATH - VERY SLOW
/mnt/batch/tasks/shared/LS_root/mounts/clusters/rarko1/code/Users/rarko/Projects/etp/etp-explanations
```

Both paths point to the same files, but `/mnt/` goes through extra network layers making git and file operations 50-100x slower.

## Initial Setup (One-Time)

### 1. Set Permissions on Management Script

```bash
cd ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations
chmod +x manage-explainer.sh
```

### 2. Fix Git Performance on Azure Files

Azure Files network mounts cause slow git operations (15+ seconds). Configure git to skip expensive filesystem scans:

```bash
cd ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations
git config --local core.fsmonitor false
git config --local core.untrackedCache true
git config --local feature.manyFiles true
git config --local index.threads 4
```

This reduces git status from ~15s to ~7s. Still slow but much better.

### 3. Install Dependencies

```bash
make install
```

### 2. Create Cache Directory

```bash
sudo mkdir -p /mnt/dqt/etp-explainer-cache
sudo chown azureuser:azureuser /mnt/dqt/etp-explainer-cache
chmod 755 /mnt/dqt/etp-explainer-cache
```

### 3. Secure Environment File

```bash
# .env contains passwords - make it readable only by owner
chmod 600 ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/.env
```

### 4. Configure Environment

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
# Edit .env with your settings (already done on rarko1)
```

Key settings in `.env`:
```bash
# Data paths
DQT_DATA_DIR=~/cloudfiles/code/Users/rarko/Projects/etp/etp-lake/data
DQT_USE_LAKE_MIRROR=1
DQT_LAKE_MIRROR=/mnt/dqt/etp_lake

# Server
EXPLAIN_HOST=0.0.0.0
EXPLAIN_PORT=8765
EXPLAIN_BEHIND_PROXY=1
EXPLAIN_CACHE_DIR=/mnt/dqt/etp-explainer-cache

# Authentication
AUTH_ENABLED=1
AUTH_USERNAME=etp
AUTH_PASSWORD='<bcrypt hash or password — never commit the real value>'
```

### 4. Configure Nginx Reverse Proxy

Create nginx config:
```bash
sudo tee /etc/nginx/sites-available/etp-explainer > /dev/null <<'EOF'
server {
    listen 80;
    server_name rarko1 10.0.0.4;

    location / {
        proxy_pass http://localhost:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Increase timeouts for long-running explanations
        proxy_connect_timeout 300;
        proxy_send_timeout 300;
        proxy_read_timeout 300;
    }
}
EOF
```

Enable site and reload nginx:
```bash
sudo ln -sf /etc/nginx/sites-available/etp-explainer /etc/nginx/sites-enabled/etp-explainer
sudo nginx -t
sudo systemctl reload nginx
```

## Daily Operations

### Start the Application

```bash
cd ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations
./manage-explainer.sh start
```

Or manually:
```bash
cd ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations
uv run python scripts/explain_serve.py > /tmp/etp-explainer.log 2>&1 &
```

### Stop the Application

```bash
./manage-explainer.sh stop
```

Or manually:
```bash
pkill -f explain_serve
```

### Restart the Application

```bash
./manage-explainer.sh restart
```

### Check Status

```bash
./manage-explainer.sh status
```

Or manually:
```bash
ps aux | grep '[p]ython.*explain_serve'
```

### View Logs

```bash
./manage-explainer.sh logs
```

Or manually:
```bash
tail -f /tmp/etp-explainer.log
```

## Access Information

### Public URL (Within Arrive Network)

```
http://rarko1/
```
or
```
http://10.0.0.4/
```

### Credentials

```
Username: etp
Password: <AUTH_PASSWORD from .env — share out of band>
```

### Endpoints

- `GET /` - Index/search page (requires auth)
- `GET /health` - Health check (no auth required)
- `GET /explain/{loadnumber}` - Load explanation page (requires auth)
  - Query params: `?refresh=true` to bypass cache, `?query_mde=true` for MDE data

## Sharing outside the VNet: Cloudflare quick tunnel

`http://rarko1/` is only reachable inside the Arrive VNet. To hand a manager or
teammate an HTTPS URL that works from anywhere (still gated by the app's basic
auth), run the share script on **either** the VM or a MacBook:

```bash
make share-check     # preflight: uv, venv, data, AUTH_PASSWORD, port, cloudflared
make share           # starts server on 127.0.0.1 + cloudflared quick tunnel, prints URL
make share-status    # URL + health
make share-stop
```

- Uses only `.env`; refuses to start if `AUTH_PASSWORD` is empty or `changeme`.
- Verifies anonymous → 401 and your creds → 200 both locally and through the tunnel.
- Quick-tunnel URLs (`https://<words>.trycloudflare.com`) change on every start
  and live only while the host is awake. For a stable hostname create a named
  tunnel in Cloudflare Zero Trust and set `SHARE_TUNNEL_TOKEN` + `SHARE_PUBLIC_URL`
  in `.env`.
- On the VM, port 8765 is normally held by the nginx-fronted instance
  (`./manage-explainer.sh`); use `make share PORT=8799` to run alongside it.
- State (pids, logs, URL) lives in `.run/` (gitignored):
  `scripts/share_explainer.sh logs [server|tunnel]`.

## Troubleshooting

### Server Won't Start

Check logs for errors:
```bash
tail -100 /tmp/etp-explainer.log | grep -i error
```

Verify data directory exists:
```bash
ls -la ~/cloudfiles/code/Users/rarko/Projects/etp/etp-lake/data/etp_lake/
```

Verify cache directory is writable:
```bash
ls -la /mnt/dqt/etp-explainer-cache
```

### 500 Internal Server Error

Check application logs:
```bash
tail -f /tmp/etp-explainer.log
```

Common issues:
- Missing data files in `DQT_DATA_DIR`
- Permissions on cache directory
- Dependency issues (run `make install` again)

### Authentication Not Working

Verify `.env` has correct credentials:
```bash
grep AUTH_ ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/.env
```

Test locally:
```bash
curl -u etp:"$AUTH_PASSWORD" http://localhost:8765/
```

### Nginx Issues

Test nginx config:
```bash
sudo nginx -t
```

Check nginx is running:
```bash
sudo systemctl status nginx
```

Reload nginx after changes:
```bash
sudo systemctl reload nginx
```

## Optional: Systemd Service (Auto-Start on Reboot)

Create service file:
```bash
sudo tee /etc/systemd/system/etp-explainer.service > /dev/null <<'EOF'
[Unit]
Description=ETP Explainer Service
After=network.target

[Service]
Type=simple
User=azureuser
WorkingDirectory=/home/azureuser/cloudfiles/code/Users/RARKO/Projects/etp/etp-explanations
Environment="PATH=/home/azureuser/uv-venvs/etp-explanations/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/home/azureuser/uv-venvs/etp-explanations/bin/python scripts/explain_serve.py
Restart=always
RestartSec=10
StandardOutput=append:/tmp/etp-explainer.log
StandardError=append:/tmp/etp-explainer.log

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable etp-explainer
sudo systemctl start etp-explainer
```

Check status:
```bash
sudo systemctl status etp-explainer
```

## File Permissions Reference

Key files and their permissions:

```bash
# Management script (executable)
chmod +x ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/manage-explainer.sh

# Cache directory (writable by azureuser)
sudo chown azureuser:azureuser /mnt/dqt/etp-explainer-cache
chmod 755 /mnt/dqt/etp-explainer-cache

# Environment file (readable only by owner, contains secrets)
chmod 600 ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/.env

# Application files (standard)
chmod 644 ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/app/**/*.py
chmod 755 ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations/scripts/*.py
```

## Updating the Application

After making code changes:

```bash
cd ~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations

# Pull latest changes
git pull

# Reinstall dependencies if needed
make install

# Restart the application
./manage-explainer.sh restart

# Verify it's working
curl -s http://localhost:8765/health
```

## Security Notes

- `.env` file is gitignored and contains credentials
- Set a strong `AUTH_PASSWORD` (bcrypt hash preferred: `uv run python -c "from app.explain.auth import hash_password; print(hash_password('...'))"`)
- The application is only accessible within the Arrive network
- For external access, configure HTTPS with SSL certificates
- Consider integrating with Arrive ID for SSO authentication (future enhancement)

## Performance Tuning

### Cache Management

Clear cache if needed:
```bash
rm -rf /mnt/dqt/etp-explainer-cache/*
```

Check cache size:
```bash
du -sh /mnt/dqt/etp-explainer-cache
```

### Adjust Timeout Settings

Edit nginx config `/etc/nginx/sites-available/etp-explainer` to increase timeouts for slow loads:
```nginx
proxy_connect_timeout 600;
proxy_send_timeout 600;
proxy_read_timeout 600;
```

Then reload nginx:
```bash
sudo systemctl reload nginx
```

## Monitoring

### Check Application Health

```bash
# Local health check
curl -s http://localhost:8765/health

# Public health check
curl -s http://10.0.0.4/health
```

### Monitor Resource Usage

```bash
# Check memory usage
ps aux | grep python | grep explain_serve

# Check disk usage
df -h /mnt/dqt/etp-explainer-cache
```

### View Access Logs

Application logs are in `/tmp/etp-explainer.log`

Nginx access logs:
```bash
sudo tail -f /var/log/nginx/access.log | grep etp-explainer
```

## Support

For issues or questions:
- Check logs: `./manage-explainer.sh logs`
- Review troubleshooting section above
- Contact: rarko@arrivelogistics.com
