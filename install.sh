#!/bin/bash

# StreamHibV2 Installation Script
# Automated installation for Debian/Ubuntu servers

set -e

echo "🚀 StreamHibV2 Installation Script"
echo "=================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    print_error "Please run this script as root or with sudo"
    exit 1
fi

# Get server IP
SERVER_IP=$(curl -s ifconfig.me || hostname -I | awk '{print $1}')

print_status "Starting installation on server: $SERVER_IP"

# Update system
print_status "Updating system packages..."
apt update && apt upgrade -y

# Install required packages
print_status "Installing required packages..."
apt install -y curl wget git ffmpeg postgresql postgresql-contrib nginx ufw

# Install Node.js 18.x
print_status "Installing Node.js..."
curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
apt install -y nodejs

# Verify installations
print_status "Verifying installations..."
node_version=$(node --version)
npm_version=$(npm --version)
ffmpeg_version=$(ffmpeg -version | head -n1)
psql_version=$(psql --version)

print_status "Node.js: $node_version"
print_status "NPM: $npm_version"
print_status "FFmpeg: $ffmpeg_version"
print_status "PostgreSQL: $psql_version"

# Configure PostgreSQL
print_status "Configuring PostgreSQL..."
systemctl start postgresql
systemctl enable postgresql

# Create database and user
DB_PASSWORD=$(openssl rand -base64 32)
sudo -u postgres psql << EOF
CREATE DATABASE streamhib_v2;
CREATE USER streamhib WITH ENCRYPTED PASSWORD '$DB_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE streamhib_v2 TO streamhib;
ALTER USER streamhib CREATEDB;
\q
EOF

print_status "Database created with user 'streamhib'"

# Create application directory
print_status "Setting up application directory..."
cd /root
if [ -d "StreamHibV2" ]; then
    print_warning "StreamHibV2 directory already exists, backing up..."
    mv StreamHibV2 StreamHibV2.backup.$(date +%Y%m%d_%H%M%S)
fi

# Clone repository (assuming the files are already in the current directory)
mkdir -p StreamHibV2
cd StreamHibV2

# Create necessary directories
mkdir -p videos videos/thumbnails logs static templates

# Install Node.js dependencies
print_status "Installing Node.js dependencies..."
npm install

# Create environment file
print_status "Creating environment configuration..."
cat > .env << EOF
NODE_ENV=production
PORT=5000
DB_HOST=localhost
DB_PORT=5432
DB_NAME=streamhib_v2
DB_USER=streamhib
DB_PASSWORD=$DB_PASSWORD
JWT_SECRET=$(openssl rand -base64 64)
BASE_URL=http://$SERVER_IP:5000

# Email configuration (configure these for password reset functionality)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASS=your-app-password
SMTP_FROM=your-email@gmail.com
EOF

print_status "Environment file created"

# Set permissions
print_status "Setting file permissions..."
chown -R root:root /root/StreamHibV2
chmod -R 755 /root/StreamHibV2
chmod 600 /root/StreamHibV2/.env

# Create systemd service
print_status "Creating systemd service..."
cat > /etc/systemd/system/streamhib-v2.service << EOF
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
EOF

# Create FFmpeg systemd template
print_status "Creating FFmpeg systemd template..."
cat > /etc/systemd/system/streamhib-stream@.service << EOF
[Unit]
Description=StreamHib Live Stream - %i
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/ffmpeg -re -stream_loop -1 -i "%i" -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ac 2 -ar 44100 -f flv "%i"
Restart=always
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd and enable service
systemctl daemon-reload
systemctl enable streamhib-v2.service

# Configure firewall
print_status "Configuring firewall..."
ufw allow 22/tcp
ufw allow 5000/tcp
ufw allow 80/tcp
ufw allow 443/tcp
echo "y" | ufw enable

# Configure Nginx (optional reverse proxy)
print_status "Configuring Nginx..."
cat > /etc/nginx/sites-available/streamhib-v2 << EOF
server {
    listen 80;
    server_name $SERVER_IP;

    location / {
        proxy_pass http://localhost:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
    }
}
EOF

ln -sf /etc/nginx/sites-available/streamhib-v2 /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# Run database migration (if old JSON files exist)
print_status "Running database migration..."
if [ -f "/root/StreamHibV2/sessions.json" ] || [ -f "/root/StreamHibV2/users.json" ]; then
    node scripts/migrate.js
    print_status "Migration completed"
else
    print_status "No existing JSON files found, skipping migration"
fi

# Start the service
print_status "Starting StreamHibV2 service..."
systemctl start streamhib-v2.service

# Wait a moment for service to start
sleep 5

# Check service status
if systemctl is-active --quiet streamhib-v2.service; then
    print_status "StreamHibV2 service is running successfully!"
else
    print_error "StreamHibV2 service failed to start. Check logs with: journalctl -u streamhib-v2.service -f"
    exit 1
fi

# Create initial admin user
print_status "Creating initial admin user..."
read -p "Enter admin email: " ADMIN_EMAIL
read -s -p "Enter admin password: " ADMIN_PASSWORD
echo

# Create user via API (service should be running)
sleep 2
curl -X POST http://localhost:5000/api/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  > /dev/null 2>&1

print_status "Admin user created successfully"

# Final status
echo ""
echo "🎉 StreamHibV2 Installation Complete!"
echo "====================================="
echo ""
print_status "Application URL: http://$SERVER_IP:5000"
print_status "Admin Email: $ADMIN_EMAIL"
echo ""
print_status "Service Management Commands:"
echo "  Start:   systemctl start streamhib-v2.service"
echo "  Stop:    systemctl stop streamhib-v2.service"
echo "  Restart: systemctl restart streamhib-v2.service"
echo "  Status:  systemctl status streamhib-v2.service"
echo "  Logs:    journalctl -u streamhib-v2.service -f"
echo ""
print_status "Database Information:"
echo "  Database: streamhib_v2"
echo "  User: streamhib"
echo "  Password: $DB_PASSWORD"
echo ""
print_warning "Important: Configure email settings in /root/StreamHibV2/.env for password reset functionality"
echo ""
print_status "Installation completed successfully! 🚀"