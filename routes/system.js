const express = require('express');
const { exec } = require('child_process');
const { promisify } = require('util');
const fs = require('fs').promises;
const path = require('path');
const db = require('../config/database');
const logger = require('../utils/logger');

const router = express.Router();
const execAsync = promisify(exec);

// Get system status
router.get('/status', async (req, res) => {
  try {
    // Get disk usage
    const { stdout: diskUsage } = await execAsync('df -h /');
    const diskLines = diskUsage.split('\n');
    const diskInfo = diskLines[1].split(/\s+/);
    
    // Get memory usage
    const { stdout: memInfo } = await execAsync('free -h');
    const memLines = memInfo.split('\n');
    const memData = memLines[1].split(/\s+/);
    
    // Get active sessions count
    const activeSessionsResult = await db.query(
      'SELECT COUNT(*) as count FROM sessions WHERE status = $1',
      ['active']
    );
    
    // Get total videos count
    const videosResult = await db.query('SELECT COUNT(*) as count FROM videos');
    
    // Get scheduled sessions count
    const schedulesResult = await db.query(
      'SELECT COUNT(*) as count FROM schedules WHERE is_active = true'
    );

    res.json({
      success: true,
      system: {
        disk: {
          total: diskInfo[1],
          used: diskInfo[2],
          available: diskInfo[3],
          usage_percent: diskInfo[4]
        },
        memory: {
          total: memData[1],
          used: memData[2],
          free: memData[3]
        },
        stats: {
          active_sessions: parseInt(activeSessionsResult.rows[0].count),
          total_videos: parseInt(videosResult.rows[0].count),
          scheduled_sessions: parseInt(schedulesResult.rows[0].count)
        }
      }
    });

  } catch (error) {
    logger.error('Get system status error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to get system status'
    });
  }
});

// Get logs
router.get('/logs', async (req, res) => {
  try {
    const { lines = 100 } = req.query;
    
    const logPath = path.join(__dirname, '../logs/combined.log');
    
    try {
      const { stdout } = await execAsync(`tail -n ${lines} "${logPath}"`);
      res.json({
        success: true,
        logs: stdout.split('\n').filter(line => line.trim())
      });
    } catch (error) {
      // If log file doesn't exist, return empty logs
      res.json({
        success: true,
        logs: []
      });
    }

  } catch (error) {
    logger.error('Get logs error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to get logs'
    });
  }
});

// Clean up old files
router.post('/cleanup', async (req, res) => {
  try {
    let cleanedFiles = 0;
    let freedSpace = 0;

    // Clean up old log files
    const logsDir = path.join(__dirname, '../logs');
    try {
      const logFiles = await fs.readdir(logsDir);
      for (const file of logFiles) {
        if (file.endsWith('.log.old') || file.endsWith('.log.1')) {
          const filePath = path.join(logsDir, file);
          const stats = await fs.stat(filePath);
          await fs.unlink(filePath);
          cleanedFiles++;
          freedSpace += stats.size;
        }
      }
    } catch (error) {
      logger.warn('Failed to clean log files:', error);
    }

    // Clean up orphaned thumbnails
    const thumbnailsDir = path.join(__dirname, '../videos/thumbnails');
    try {
      const thumbnailFiles = await fs.readdir(thumbnailsDir);
      const videoResult = await db.query('SELECT thumbnail_path FROM videos WHERE thumbnail_path IS NOT NULL');
      const validThumbnails = videoResult.rows.map(row => path.basename(row.thumbnail_path));
      
      for (const file of thumbnailFiles) {
        if (!validThumbnails.includes(file)) {
          const filePath = path.join(thumbnailsDir, file);
          const stats = await fs.stat(filePath);
          await fs.unlink(filePath);
          cleanedFiles++;
          freedSpace += stats.size;
        }
      }
    } catch (error) {
      logger.warn('Failed to clean thumbnail files:', error);
    }

    res.json({
      success: true,
      message: `Cleaned up ${cleanedFiles} files, freed ${(freedSpace / 1024 / 1024).toFixed(2)} MB`
    });

  } catch (error) {
    logger.error('Cleanup error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to perform cleanup'
    });
  }
});

module.exports = router;