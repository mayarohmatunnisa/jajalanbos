const { exec } = require('child_process');
const { promisify } = require('util');
const fs = require('fs').promises;
const path = require('path');
const { v4: uuidv4 } = require('uuid');
const logger = require('../utils/logger');

const execAsync = promisify(exec);

// Platform RTMP URLs
const PLATFORM_URLS = {
  'youtube': 'rtmp://a.rtmp.youtube.com/live2/',
  'facebook': 'rtmps://live-api-s.facebook.com:443/rtmp/',
  'twitch': 'rtmp://live.twitch.tv/app/',
  'instagram': 'rtmp://live-upload.instagram.com/rtmp/',
  'tiktok': 'rtmp://push.live.tiktok.com/live/',
  'custom': '' // For custom RTMP URLs
};

async function startStream(session) {
  try {
    const serviceName = `stream-${session.id}-${uuidv4().substring(0, 8)}`;
    const rtmpUrl = PLATFORM_URLS[session.platform.toLowerCase()] || session.platform;
    const fullRtmpUrl = rtmpUrl + session.stream_key;

    // Create systemd service file
    const serviceContent = `[Unit]
Description=StreamHib Live Stream - ${session.filename}
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/ffmpeg -re -stream_loop -1 -i "${session.file_path}" -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ac 2 -ar 44100 -f flv "${fullRtmpUrl}"
Restart=always
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
`;

    const servicePath = `/etc/systemd/system/${serviceName}.service`;
    await fs.writeFile(servicePath, serviceContent);

    // Reload systemd and start service
    await execAsync('systemctl daemon-reload');
    await execAsync(`systemctl enable ${serviceName}.service`);
    await execAsync(`systemctl start ${serviceName}.service`);

    // Get PID
    const { stdout } = await execAsync(`systemctl show --property MainPID ${serviceName}.service`);
    const pid = parseInt(stdout.split('=')[1]);

    logger.info(`Started stream service: ${serviceName}, PID: ${pid}`);

    return {
      serviceName,
      pid
    };

  } catch (error) {
    logger.error('Failed to start stream:', error);
    throw new Error(`Failed to start stream: ${error.message}`);
  }
}

async function stopStream(session) {
  try {
    if (!session.service_name) {
      throw new Error('No service name found for session');
    }

    // Stop and disable service
    await execAsync(`systemctl stop ${session.service_name}.service`);
    await execAsync(`systemctl disable ${session.service_name}.service`);

    // Remove service file
    const servicePath = `/etc/systemd/system/${session.service_name}.service`;
    try {
      await fs.unlink(servicePath);
    } catch (error) {
      logger.warn('Failed to remove service file:', error);
    }

    // Reload systemd
    await execAsync('systemctl daemon-reload');

    logger.info(`Stopped stream service: ${session.service_name}`);

  } catch (error) {
    logger.error('Failed to stop stream:', error);
    throw new Error(`Failed to stop stream: ${error.message}`);
  }
}

async function getStreamStatus(serviceName) {
  try {
    const { stdout } = await execAsync(`systemctl is-active ${serviceName}.service`);
    return stdout.trim() === 'active';
  } catch (error) {
    return false;
  }
}

module.exports = {
  startStream,
  stopStream,
  getStreamStatus
};