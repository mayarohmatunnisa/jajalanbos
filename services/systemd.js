const fs = require('fs').promises;
const { exec } = require('child_process');
const { promisify } = require('util');
const logger = require('../utils/logger');

const execAsync = promisify(exec);

async function initializeSystemdServices() {
  try {
    // Create systemd template for streams
    const templateContent = `[Unit]
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
`;

    await fs.writeFile('/etc/systemd/system/streamhib@.service', templateContent);
    await execAsync('systemctl daemon-reload');
    
    logger.info('Systemd services initialized');

  } catch (error) {
    logger.error('Failed to initialize systemd services:', error);
    throw error;
  }
}

async function createMainService() {
  try {
    const serviceContent = `[Unit]
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
`;

    await fs.writeFile('/etc/systemd/system/streamhib-v2.service', serviceContent);
    await execAsync('systemctl daemon-reload');
    await execAsync('systemctl enable streamhib-v2.service');
    
    logger.info('Main service created and enabled');

  } catch (error) {
    logger.error('Failed to create main service:', error);
    throw error;
  }
}

module.exports = {
  initializeSystemdServices,
  createMainService
};