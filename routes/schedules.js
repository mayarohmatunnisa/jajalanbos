const express = require('express');
const moment = require('moment-timezone');
const db = require('../config/database');
const { scheduleSession, cancelSchedule } = require('../services/scheduler');
const logger = require('../utils/logger');

const router = express.Router();

// Get all schedules
router.get('/', async (req, res) => {
  try {
    const result = await db.query(`
      SELECT sc.*, s.stream_key, s.platform, v.filename, v.original_name
      FROM schedules sc
      JOIN sessions s ON sc.session_id = s.id
      LEFT JOIN videos v ON s.video_id = v.id
      WHERE sc.is_active = true
      ORDER BY sc.next_run ASC
    `);

    res.json({
      success: true,
      schedules: result.rows
    });

  } catch (error) {
    logger.error('Get schedules error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to fetch schedules'
    });
  }
});

// Create schedule
router.post('/', async (req, res) => {
  try {
    const { 
      session_id, 
      schedule_type, 
      start_datetime, 
      end_datetime, 
      timezone = 'UTC' 
    } = req.body;

    if (!session_id || !schedule_type || !start_datetime || !end_datetime) {
      return res.status(400).json({
        success: false,
        message: 'Session ID, schedule type, start and end datetime are required'
      });
    }

    // Verify session exists and is inactive
    const sessionResult = await db.query(
      'SELECT * FROM sessions WHERE id = $1 AND status = $2',
      [session_id, 'inactive']
    );

    if (sessionResult.rows.length === 0) {
      return res.status(400).json({
        success: false,
        message: 'Session not found or not inactive'
      });
    }

    // Convert times to UTC
    const startUTC = moment.tz(start_datetime, timezone).utc().toDate();
    const endUTC = moment.tz(end_datetime, timezone).utc().toDate();

    // Calculate next run time
    let nextRun = startUTC;
    if (schedule_type === 'daily') {
      // For daily schedules, if start time has passed today, schedule for tomorrow
      const now = new Date();
      if (startUTC <= now) {
        nextRun = new Date(startUTC.getTime() + 24 * 60 * 60 * 1000);
      }
    }

    // Create schedule
    const result = await db.query(`
      INSERT INTO schedules (session_id, schedule_type, start_datetime, end_datetime, timezone, next_run)
      VALUES ($1, $2, $3, $4, $5, $6)
      RETURNING *
    `, [session_id, schedule_type, startUTC, endUTC, timezone, nextRun]);

    const schedule = result.rows[0];

    // Schedule the job
    await scheduleSession(schedule);

    req.io.emit('schedule_created', schedule);

    res.json({
      success: true,
      schedule
    });

  } catch (error) {
    logger.error('Create schedule error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to create schedule'
    });
  }
});

// Update schedule
router.put('/:id', async (req, res) => {
  try {
    const { id } = req.params;
    const { 
      start_datetime, 
      end_datetime, 
      timezone = 'UTC' 
    } = req.body;

    // Get existing schedule
    const existingResult = await db.query('SELECT * FROM schedules WHERE id = $1', [id]);
    if (existingResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Schedule not found'
      });
    }

    const existingSchedule = existingResult.rows[0];

    // Cancel existing schedule
    await cancelSchedule(existingSchedule);

    // Convert times to UTC
    const startUTC = moment.tz(start_datetime, timezone).utc().toDate();
    const endUTC = moment.tz(end_datetime, timezone).utc().toDate();

    // Calculate next run time
    let nextRun = startUTC;
    if (existingSchedule.schedule_type === 'daily') {
      const now = new Date();
      if (startUTC <= now) {
        nextRun = new Date(startUTC.getTime() + 24 * 60 * 60 * 1000);
      }
    }

    // Update schedule
    const result = await db.query(`
      UPDATE schedules 
      SET start_datetime = $1, end_datetime = $2, timezone = $3, next_run = $4, updated_at = CURRENT_TIMESTAMP
      WHERE id = $5
      RETURNING *
    `, [startUTC, endUTC, timezone, nextRun, id]);

    const updatedSchedule = result.rows[0];

    // Reschedule the job
    await scheduleSession(updatedSchedule);

    req.io.emit('schedule_updated', updatedSchedule);

    res.json({
      success: true,
      schedule: updatedSchedule
    });

  } catch (error) {
    logger.error('Update schedule error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to update schedule'
    });
  }
});

// Delete schedule
router.delete('/:id', async (req, res) => {
  try {
    const { id } = req.params;

    // Get schedule
    const scheduleResult = await db.query('SELECT * FROM schedules WHERE id = $1', [id]);
    if (scheduleResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        message: 'Schedule not found'
      });
    }

    const schedule = scheduleResult.rows[0];

    // Cancel schedule
    await cancelSchedule(schedule);

    // Delete from database
    await db.query('DELETE FROM schedules WHERE id = $1', [id]);

    req.io.emit('schedule_deleted', { id });

    res.json({
      success: true,
      message: 'Schedule deleted successfully'
    });

  } catch (error) {
    logger.error('Delete schedule error:', error);
    res.status(500).json({
      success: false,
      message: 'Failed to delete schedule'
    });
  }
});

module.exports = router;