const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs').promises;
const { exec } = require('child_process');
const { promisify } = require('util');
const db = require('../config/database');
const logger = require('../utils/logger');

const router = express.Router();
const execAsync = promisify(exec);

// Configure multer for video uploads
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    cb(null, 'videos/');
  },
  filename: (req, file, cb) => {
    const uniqueSuffix = Date.now() + '-' + Math.round(Math.random() * 1E9);
    cb(null, uniqueSuffix + path.extname(file.originalname));
  }
});

const upload = multer({ 
  storage,
  limits: { fileSize: 10 * 1024 * 1024 * 1024 }, // 10GB limit
  fileFilter: (req, file, cb) => {
    const allowedTypes = /\.(mp4|avi|mkv|mov|wmv|flv|webm)$/i;
    if (allowedTypes.test(file.originalname)) {
      cb(null, true);
    } else {
      cb(new Error('Invalid file type. Only video files are allowed.'));
    }
  }
});

// Get all videos
router.get('/', async (req, res) => {
  try {
    const result = await db.query(`
      SELECT v.*, 
             COUNT(s.id) as session_count,
             MAX(s.created_at) as last_session
      FROM videos v
      LEFT JOIN sessions s ON v.id = s.video_id
      GROUP BY v.id
      ORDER BY v.created_at DESC
    `);

    res.json({
      success: true,
      videos: result.rows
    });

  } catch (error) {
    logger.error('Get videos error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to fetch videos'
    });
  }
});

// Upload video
router.post('/upload', upload.single('video'), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({
        success: false,
        message: 'No video file provided'
      });
    }

    const { originalname, filename, path: filePath, size } = req.file;

    // Get video duration using ffprobe
    let duration = null;
    try {
      const { stdout } = await execAsync(
        `ffprobe -v quiet -show_entries format=duration -of csv=p=0 "${filePath}"`
      );
      duration = Math.round(parseFloat(stdout.trim()));
    } catch (error) {
      logger.warn('Failed to get video duration:', error);
    }

    // Generate thumbnail
    const thumbnailPath = `videos/thumbnails/${filename.replace(/\.[^/.]+$/, '')}.jpg`;
    try {
      await fs.mkdir('videos/thumbnails', { recursive: true });
      await execAsync(
        `ffmpeg -i "${filePath}" -ss 00:00:01 -vframes 1 -y "${thumbnailPath}"`
      );
    } catch (error) {
      logger.warn('Failed to generate thumbnail:', error);
    }

    // Save to database
    const result = await db.query(`
      INSERT INTO videos (filename, original_name, file_path, file_size, duration, thumbnail_path)
      VALUES ($1, $2, $3, $4, $5, $6)
      RETURNING *
    `, [filename, originalname, filePath, size, duration, thumbnailPath]);

    req.io.emit('video_uploaded', result.rows[0]);

    res.json({
      success: true,
      video: result.rows[0]
    });

  } catch (error) {
    logger.error('Upload video error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to upload video'
    });
  }
});

// Download video from Google Drive
router.post('/download', async (req, res) => {
  try {
    const { url, filename } = req.body;

    if (!url || !filename) {
      return res.status(400).json({
        success: false,
        message: 'URL and filename are required'
      });
    }

    // Extract file ID from Google Drive URL
    const fileIdMatch = url.match(/\/d\/([a-zA-Z0-9-_]+)/);
    if (!fileIdMatch) {
      return res.status(400).json({
        success: false,
        message: 'Invalid Google Drive URL'
      });
    }

    const fileId = fileIdMatch[1];
    const sanitizedFilename = filename.replace(/[^a-zA-Z0-9.-]/g, '_');
    const outputPath = `videos/${sanitizedFilename}`;

    // Start download process
    req.io.emit('download_started', { filename: sanitizedFilename });

    try {
      await execAsync(`gdown ${fileId} -O "${outputPath}"`);
      
      // Get file stats
      const stats = await fs.stat(outputPath);
      
      // Get video duration
      let duration = null;
      try {
        const { stdout } = await execAsync(
          `ffprobe -v quiet -show_entries format=duration -of csv=p=0 "${outputPath}"`
        );
        duration = Math.round(parseFloat(stdout.trim()));
      } catch (error) {
        logger.warn('Failed to get video duration:', error);
      }

      // Generate thumbnail
      const thumbnailPath = `videos/thumbnails/${sanitizedFilename.replace(/\.[^/.]+$/, '')}.jpg`;
      try {
        await fs.mkdir('videos/thumbnails', { recursive: true });
        await execAsync(
          `ffmpeg -i "${outputPath}" -ss 00:00:01 -vframes 1 -y "${thumbnailPath}"`
        );
      } catch (error) {
        logger.warn('Failed to generate thumbnail:', error);
      }

      // Save to database
      const result = await db.query(`
        INSERT INTO videos (filename, original_name, file_path, file_size, duration, thumbnail_path)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
      `, [sanitizedFilename, filename, outputPath, stats.size, duration, thumbnailPath]);

      req.io.emit('download_completed', result.rows[0]);

      res.json({
        success: true,
        video: result.rows[0]
      });

    } catch (downloadError) {
      req.io.emit('download_failed', { filename: sanitizedFilename, error: downloadError.message });
      throw downloadError;
    }

  } catch (error) {
    logger.error('Download video error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to download video'
    });
  }
});

// Rename video
router.put('/:id/rename', async (req, res) => {
  try {
    const { id } = req.params;
    const { newName } = req.body;

    if (!newName) {
      return res.status(400).json({
        success: false,
        message: 'New name is required'
      });
    }

    // Get current video info
    const videoResult = await db.query('SELECT * FROM videos WHERE id = $1', [id]);
    if (videoResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Video not found'
      });
    }

    const video = videoResult.rows[0];
    const oldPath = video.file_path;
    const extension = path.extname(oldPath);
    const sanitizedName = newName.replace(/[^a-zA-Z0-9.-]/g, '_');
    const newFilename = sanitizedName + extension;
    const newPath = path.join('videos', newFilename);

    // Rename file
    await fs.rename(oldPath, newPath);

    // Update database
    const result = await db.query(`
      UPDATE videos 
      SET filename = $1, file_path = $2, updated_at = CURRENT_TIMESTAMP
      WHERE id = $3
      RETURNING *
    `, [newFilename, newPath, id]);

    req.io.emit('video_renamed', result.rows[0]);

    res.json({
      success: true,
      video: result.rows[0]
    });

  } catch (error) {
    logger.error('Rename video error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to rename video'
    });
  }
});

// Delete video
router.delete('/:id', async (req, res) => {
  try {
    const { id } = req.params;

    // Get video info
    const videoResult = await db.query('SELECT * FROM videos WHERE id = $1', [id]);
    if (videoResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Video not found'
      });
    }

    const video = videoResult.rows[0];

    // Check if video is being used in active sessions
    const activeSessionsResult = await db.query(
      'SELECT COUNT(*) as count FROM sessions WHERE video_id = $1 AND status = $2',
      [id, 'active']
    );

    if (parseInt(activeSessionsResult.rows[0].count) > 0) {
      return res.status(400).json({
        success: false,
        message: 'Cannot delete video with active sessions'
      });
    }

    // Delete file
    try {
      await fs.unlink(video.file_path);
    } catch (error) {
      logger.warn('Failed to delete video file:', error);
    }

    // Delete thumbnail
    if (video.thumbnail_path) {
      try {
        await fs.unlink(video.thumbnail_path);
      } catch (error) {
        logger.warn('Failed to delete thumbnail:', error);
      }
    }

    // Delete from database (cascades to sessions and schedules)
    await db.query('DELETE FROM videos WHERE id = $1', [id]);

    req.io.emit('video_deleted', { id });

    res.json({
      success: true,
      message: 'Video deleted successfully'
    });

  } catch (error) {
    logger.error('Delete video error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to delete video'
    });
  }
});

module.exports = router;