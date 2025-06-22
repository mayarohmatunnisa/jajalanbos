const express = require('express');
const db = require('../config/database');
const { startStream, stopStream } = require('../services/streaming');
const logger = require('../utils/logger');

const router = express.Router();

// Get all sessions
router.get('/', async (req, res) => {
  try {
    const result = await db.query(`
      SELECT s.*, v.filename, v.original_name, v.thumbnail_path
      FROM sessions s
      LEFT JOIN videos v ON s.video_id = v.id
      ORDER BY s.created_at DESC
    `);

    res.json({
      success: true,
      sessions: result.rows
    });

  } catch (error) {
    logger.error('Get sessions error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to fetch sessions'
    });
  }
});

// Get active sessions
router.get('/active', async (req, res) => {
  try {
    const result = await db.query(`
      SELECT s.*, v.filename, v.original_name, v.thumbnail_path
      FROM sessions s
      LEFT JOIN videos v ON s.video_id = v.id
      WHERE s.status = 'active'
      ORDER BY s.start_time DESC
    `);

    res.json({
      success: true,
      sessions: result.rows
    });

  } catch (error) {
    logger.error('Get active sessions error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to fetch active sessions'
    });
  }
});

// Get inactive sessions
router.get('/inactive', async (req, res) => {
  try {
    const result = await db.query(`
      SELECT s.*, v.filename, v.original_name, v.thumbnail_path
      FROM sessions s
      LEFT JOIN videos v ON s.video_id = v.id
      WHERE s.status = 'inactive'
      ORDER BY s.created_at DESC
    `);

    res.json({
      success: true,
      sessions: result.rows
    });

  } catch (error) {
    logger.error('Get inactive sessions error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to fetch inactive sessions'
    });
  }
});

// Create new session
router.post('/', async (req, res) => {
  try {
    const { video_id, stream_key, platform } = req.body;

    if (!video_id || !stream_key || !platform) {
      return res.status(400).json({
        success: false,
        message: 'Video ID, stream key, and platform are required'
      });
    }

    // Verify video exists
    const videoResult = await db.query('SELECT * FROM videos WHERE id = $1', [video_id]);
    if (videoResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Video not found'
      });
    }

    // Create session
    const result = await db.query(`
      INSERT INTO sessions (video_id, stream_key, platform, status)
      VALUES ($1, $2, $3, 'inactive')
      RETURNING *
    `, [video_id, stream_key, platform]);

    const session = result.rows[0];

    req.io.emit('session_created', session);

    res.json({
      success: true,
      session
    });

  } catch (error) {
    logger.error('Create session error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to create session'
    });
  }
});

// Start session
router.post('/:id/start', async (req, res) => {
  try {
    const { id } = req.params;

    // Get session with video info
    const result = await db.query(`
      SELECT s.*, v.file_path, v.filename
      FROM sessions s
      JOIN videos v ON s.video_id = v.id
      WHERE s.id = $1
    `, [id]);

    if (result.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Session not found'
      });
    }

    const session = result.rows[0];

    if (session.status === 'active') {
      return res.status(400).json({
        success: false,
        message: 'Session is already active'
      });
    }

    // Start streaming
    const streamResult = await startStream(session);

    // Update session in database
    const updatedSession = await db.query(`
      UPDATE sessions 
      SET status = 'active', 
          service_name = $1, 
          pid = $2, 
          start_time = CURRENT_TIMESTAMP,
          updated_at = CURRENT_TIMESTAMP
      WHERE id = $3
      RETURNING *
    `, [streamResult.serviceName, streamResult.pid, id]);

    req.io.emit('session_started', updatedSession.rows[0]);

    res.json({
      success: true,
      session: updatedSession.rows[0]
    });

  } catch (error) {
    logger.error('Start session error:', error);
    res.status(500).json({
      success: false,
      message: error.message || 'Failed to start session'
    });
  }
});

// Stop session
router.post('/:id/stop', async (req, res) => {
  try {
    const { id } = req.params;

    // Get session
    const result = await db.query('SELECT * FROM sessions WHERE id = $1', [id]);
    if (result.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Session not found'
      });
    }

    const session = result.rows[0];

    if (session.status !== 'active') {
      return res.status(400).json({
        success: false,
        message: 'Session is not active'
      });
    }

    // Stop streaming
    await stopStream(session);

    // Update session in database
    const updatedSession = await db.query(`
      UPDATE sessions 
      SET status = 'inactive', 
          service_name = NULL, 
          pid = NULL, 
          end_time = CURRENT_TIMESTAMP,
          updated_at = CURRENT_TIMESTAMP
      WHERE id = $1
      RETURNING *
    `, [id]);

    req.io.emit('session_stopped', updatedSession.rows[0]);

    res.json({
      success: true,
      session: updatedSession.rows[0]
    });

  } catch (error) {
    logger.error('Stop session error:', error);
    res.status(500).json({
      success: false,
      message: error.message || 'Failed to stop session'
    });
  }
});

// Update session
router.put('/:id', async (req, res) => {
  try {
    const { id } = req.params;
    const { stream_key, platform } = req.body;

    // Check if session exists and is not active
    const sessionResult = await db.query('SELECT * FROM sessions WHERE id = $1', [id]);
    if (sessionResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Session not found'
      });
    }

    if (sessionResult.rows[0].status === 'active') {
      return res.status(400).json({
        success: false,
        message: 'Cannot update active session'
      });
    }

    // Update session
    const result = await db.query(`
      UPDATE sessions 
      SET stream_key = $1, platform = $2, updated_at = CURRENT_TIMESTAMP
      WHERE id = $3
      RETURNING *
    `, [stream_key, platform, id]);

    req.io.emit('session_updated', result.rows[0]);

    res.json({
      success: true,
      session: result.rows[0]
    });

  } catch (error) {
    logger.error('Update session error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to update session'
    });
  }
});

// Delete session
router.delete('/:id', async (req, res) => {
  try {
    const { id } = req.params;

    // Check if session exists and is not active
    const sessionResult = await db.query('SELECT * FROM sessions WHERE id = $1', [id]);
    if (sessionResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Session not found'
      });
    }

    if (sessionResult.rows[0].status === 'active') {
      return res.status(400).json({
        success: false,
        message: 'Cannot delete active session'
      });
    }

    // Delete session (cascades to schedules)
    await db.query('DELETE FROM sessions WHERE id = $1', [id]);

    req.io.emit('session_deleted', { id });

    res.json({
      success: true,
      message: 'Session deleted successfully'
    });

  } catch (error) {
    logger.error('Delete session error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to delete session'
    });
  }
});

module.exports = router;