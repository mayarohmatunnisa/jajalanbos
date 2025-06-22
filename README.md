# StreamHibV2 - Node.js Migration

> **Release Date**: 31/05/2025  
> **Status**: Migrated to Node.js with PostgreSQL  
> **Function**: Live streaming management platform based on Node.js + FFmpeg, suitable for Regxa, Contabo, Hetzner, and Biznet servers.

---

## ✨ Key Features

* Web-based streaming control panel (Node.js + Express)
* Equipped with Socket.IO and FFmpeg
* Easy and fast installation on Debian/Ubuntu VPS
* Autostart service via `systemd`
* PostgreSQL database for robust data storage
* Email-based authentication with password recovery
* Video preview thumbnails
* Improved UI/UX for inactive sessions
* Persistent scheduling system
* Transfer and backup videos from old servers

---

## 🧱 Prerequisites

* Debian-based server (Regxa, Contabo, Hetzner, Biznet)
* Root access or user with sudo privileges
* Active SSH key (specifically for Biznet)
* Port 5000 open for public access
* PostgreSQL 12+ support

---

## 🚀 Complete Installation

### Automated Installation (Recommended)

```bash
# Download and run the installation script
curl -fsSL https://raw.githubusercontent.com/emuhib/StreamHibV2/main/install.sh | sudo bash
```

### Manual Installation

#### 1. Initial Setup (Optional depending on provider)

##### a. For Regxa / Contabo

```bash
apt update && apt install sudo -y
```

##### b. For Biznet

Login as user (`emuhib`) then enter root:

```bash
sudo su
```

Edit SSH config:

```bash
nano /etc/ssh/sshd_config
```

Change the following lines:

```
PermitRootLogin yes
PasswordAuthentication yes
```

---

#### 2. Update System & Install Dependencies

```bash
sudo apt update && sudo apt upgrade -y && sudo apt dist-upgrade -y
```

```bash 
sudo apt install -y curl wget git ffmpeg postgresql postgresql-contrib nginx ufw
```

```bash 
# Install Node.js 18.x
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs
```

---

#### 3. Configure PostgreSQL

```bash
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Create database and user
sudo -u postgres psql << EOF
CREATE DATABASE streamhib_v2;
CREATE USER streamhib WITH ENCRYPTED PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE streamhib_v2 TO streamhib;
ALTER USER streamhib CREATEDB;
\q
EOF
```

---

#### 4. Clone Repository

```bash
cd /root
git clone https://github.com/emuhib/StreamHibV2.git
cd StreamHibV2
```

---

#### 5. Install Node.js Dependencies

```bash
npm install
```

---

#### 6. Configure Environment

```bash
cp .env.example .env
nano .env
```

Update the environment variables:

```env
NODE_ENV=production
PORT=5000
DB_HOST=localhost
DB_PORT=5432
DB_NAME=streamhib_v2
DB_USER=streamhib
DB_PASSWORD=your_secure_password
JWT_SECRET=your_jwt_secret_key
BASE_URL=http://your-server-ip:5000

# Email configuration for password reset
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASS=your-app-password
SMTP_FROM=your-email@gmail.com
```

---

#### 7. Configure Firewall

```bash
sudo ufw allow 22/tcp
sudo ufw allow 5000/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

---

#### 8. Configure Systemd Service

```bash
sudo nano /etc/systemd/system/streamhib-v2.service
```

Service file content:

```ini
[Unit]
Description=StreamHibV2 Node.js Service
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/StreamHibV2
Environment=NODE_ENV=production
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

#### 9. Start & Enable Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable streamhib-v2.service
sudo systemctl start streamhib-v2.service
```

---

## 🔍 Additional Commands

* **Check status**:
  `sudo systemctl status streamhib-v2.service`

* **Stop Service**:
  `sudo systemctl stop streamhib-v2.service`

* **Restart Service**:
  ```bash
  sudo systemctl restart streamhib-v2.service
  ```
 
* **Check Logs**:
  `journalctl -u streamhib-v2.service -f`

* **Manual Test (Without systemd)**:
  ```bash
  cd /root/StreamHibV2
  npm start
  ```

---

## 🔄 Migration from Python Version

If you have an existing Python-based StreamHib installation:

```bash
# Run the migration script
node scripts/migrate.js
```

This will automatically migrate:
- User accounts from `users.json`
- Session data from `sessions.json`
- Video metadata
- Schedule configurations

---

## 🛠 Troubleshooting

### 1. Database Connection Issues

Check PostgreSQL status:
```bash
sudo systemctl status postgresql
sudo -u postgres psql -c "SELECT version();"
```

### 2. Permission Issues

Ensure proper file permissions:
```bash
sudo chown -R root:root /root/StreamHibV2
sudo chmod -R 755 /root/StreamHibV2
sudo chmod 600 /root/StreamHibV2/.env
```

### 3. Email Configuration

For Gmail SMTP, use App Passwords:
1. Enable 2-factor authentication
2. Generate an App Password
3. Use the App Password in `SMTP_PASS`

### 4. FFmpeg Issues

Verify FFmpeg installation:
```bash
ffmpeg -version
which ffmpeg
```

### 5. Port Already in Use

Check what's using port 5000:
```bash
sudo lsof -i :5000
sudo netstat -tulpn | grep :5000
```

---

## 🆕 New Features in V2

### Enhanced Authentication
- Email-based login system
- Password recovery via email
- Secure JWT token authentication
- Session management

### Improved Database
- PostgreSQL for robust data storage
- ACID compliance and data integrity
- Better performance and scalability
- Automatic schema initialization

### Better UI/UX
- Video preview thumbnails
- Direct action buttons for inactive sessions
- Real-time updates via Socket.IO
- Responsive design improvements

### Robust Scheduling
- Persistent scheduling with database storage
- Recovery after application restarts
- Support for daily and one-time schedules
- Timezone-aware scheduling

### System Management
- Disk usage monitoring
- System cleanup utilities
- Comprehensive logging
- Health status monitoring

---

## ✅ Complete!

Access the application through your browser:

```
http://<Server-IP>:5000
```

StreamHibV2 is ready for your live streaming needs!

---

## 📧 Support

For issues and support, please check the logs first:

```bash
journalctl -u streamhib-v2.service -f
```

Common log locations:
- Application logs: `/root/StreamHibV2/logs/`
- System logs: `journalctl -u streamhib-v2.service`
- PostgreSQL logs: `/var/log/postgresql/`

---