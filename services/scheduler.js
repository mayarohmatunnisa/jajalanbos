const cron = require('node-cron');
const moment = require('moment-timezone');
const db = require('../config/database');
const { startStream, stopStream } = require('./streaming');
const logger = require('../utils/logger');

let scheduledJobs = new Map();
let io;

function initializeScheduler(socketIo) {
  io = socketIo;
  
  // Load existing schedules from database
  loadSchedulesFromDatabase();
  
  // Check for missed schedules every minute
  cron.schedule('* * * * *', checkMissedSchedules);
  
  logger.info('Scheduler initialized');
}

async function loadSchedulesFromDatabase() {
  try {
    const result = await db.query(`
      SELECT sc.*, s.*, v.file_path, v.filename
      FROM schedules sc
      JOIN sessions s ON sc.session_id = s.id
      JOIN videos v ON s.video_id = v.id
      WHERE sc.is_active = true AND sc.next_run > NOW()
    `);

    for (const schedule of result.rows) {
      await scheduleSession(schedule);
    }

    logger.info(`Loaded ${result.rows.length} schedules from database`);

  } catch (error) {
    logger.error('Failed to load schedules from database:', error);
  }
}

async function scheduleSession(schedule) {
  try {
    const scheduleKey = `schedule-${schedule.id}`;
    
    // Cancel existing job if any
    if (scheduledJobs.has(scheduleKey)) {
      scheduledJobs.get(scheduleKey).destroy();
      scheduledJobs.delete(scheduleKey);
    }

    const startTime = moment(schedule.next_run);
    const endTime = moment(schedule.end_datetime);
    
    // Schedule start
    const startCron = `${startTime.minute()} ${startTime.hour()} ${startTime.date()} ${startTime.month() + 1} *`;
    const startJob = cron.schedule(startCron, async () => {
      await executeScheduledStart(schedule);
    }, {
      scheduled: false,
      timezone: schedule.timezone
    });

    // Schedule stop
    const stopCron = `${endTime.minute()} ${endTime.hour()} ${endTime.date()} ${endTime.month() + 1} *`;
    const stopJob = cron.schedule(stopCron, async () => {
      await executeScheduledStop(schedule);
    }, {
      scheduled: false,
      timezone: schedule.timezone
    });

    startJob.start();
    stopJob.start();

    scheduledJobs.set(scheduleKey, { startJob, stopJob });
    
    logger.info(`Scheduled session ${schedule.session_id} to start at ${startTime.format()}`);

  } catch (error) {
    logger.error('Failed to schedule session:', error);
    throw error;
  }
}

async function executeScheduledStart(schedule) {
  try {
    logger.info(`Executing scheduled start for session ${schedule.session_id}`);

    // Check if session is still inactive
    const sessionResult = await db.query(
      'SELECT * FROM sessions WHERE id = $1 AND status = $2',
      [schedule.session_id, 'inactive']
    );

    if (sessionResult.rows.length === 0) {
      logger.warn(`Session ${schedule.session_id} is not available for scheduled start`);
      return;
    }

    const session = sessionResult.rows[0];

    // Start the stream
    const streamResult = await startStream({
      ...session,
      file_path: schedule.file_path,
      filename: schedule.filename
    });

    // Update session status
    await db.query(`
      UPDATE sessions 
      SET status = 'active', 
          service_name = $1, 
          pid = $2, 
          start_time = CURRENT_TIMESTAMP,
          updated_at = CURRENT_TIMESTAMP
      WHERE id = $3
    `, [streamResult.serviceName, streamResult.pid, schedule.session_id]);

    // Update schedule last run
    await db.query(
      'UPDATE schedules SET last_run = CURRENT_TIMESTAMP WHERE id = $1',
      [schedule.id]
    );

    // If daily schedule, calculate next run
    if (schedule.schedule_type === 'daily') {
      const nextRun = moment(schedule.next_run).add(1, 'day').toDate();
      await db.query(
        'UPDATE schedules SET next_run = $1 WHERE id = $2',
        [nextRun, schedule.id]
      );
      
      // Reschedule for next day
      const updatedSchedule = { ...schedule, next_run: nextRun };
      await scheduleSession(updatedSchedule);
    } else {
      // One-time schedule, mark as inactive
      await db.query(
        'UPDATE schedules SET is_active = false WHERE id = $1',
        [schedule.id]
      );
    }

    if (io) {
      io.emit('scheduled_session_started', { sessionId: schedule.session_id });
    }

    logger.info(`Successfully started scheduled session ${schedule.session_id}`);

  } catch (error) {
    logger.error(`Failed to execute scheduled start for session ${schedule.session_id}:`, error);
    
    if (io) {
      io.emit('scheduled_session_failed', { 
        sessionId: schedule.session_id, 
        error: error.message 
      });
    }
  }
}

async function executeScheduledStop(schedule) {
  try {
    logger.info(`Executing scheduled stop for session ${schedule.session_id}`);

    // Get current session status
    const sessionResult = await db.query(
      'SELECT * FROM sessions WHERE id = $1',
      [schedule.session_id]
    );

    if (sessionResult.rows.length === 0) {
      logger.warn(`Session ${schedule.session_id} not found for scheduled stop`);
      return;
    }

    const session = sessionResult.rows[0];

    if (session.status === 'active' && session.service_name) {
      // Stop the stream
      await stopStream(session);

      // Update session status
      await db.query(`
        UPDATE sessions 
        SET status = 'inactive', 
            service_name = NULL, 
            pid = NULL, 
            end_time = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $1
      `, [schedule.session_id]);

      if (io) {
        io.emit('scheduled_session_stopped', { sessionId: schedule.session_id });
      }

      logger.info(`Successfully stopped scheduled session ${schedule.session_id}`);
    }

  } catch (error) {
    logger.error(`Failed to execute scheduled stop for session ${schedule.session_id}:`, error);
  }
}

async function checkMissedSchedules() {
  try {
    const now = new Date();
    const fiveMinutesAgo = new Date(now.getTime() - 5 * 60 * 1000);

    // Find schedules that should have started but didn't
    const missedResult = await db.query(`
      SELECT sc.*, s.*, v.file_path, v.filename
      FROM schedules sc
      JOIN sessions s ON sc.session_id = s.id
      JOIN videos v ON s.video_id = v.id
      WHERE sc.is_active = true 
        AND sc.next_run BETWEEN $1 AND $2
        AND s.status = 'inactive'
    `, [fiveMinutesAgo, now]);

    for (const schedule of missedResult.rows) {
      logger.warn(`Found missed schedule for session ${schedule.session_id}, executing now`);
      await executeScheduledStart(schedule);
    }

  } catch (error) {
    logger.error('Error checking missed schedules:', error);
  }
}

async function cancelSchedule(schedule) {
  const scheduleKey = `schedule-${schedule.id}`;
  
  if (scheduledJobs.has(scheduleKey)) {
    const jobs = scheduledJobs.get(scheduleKey);
    if (jobs.startJob) jobs.startJob.destroy();
    if (jobs.stopJob) jobs.stopJob.destroy();
    scheduledJobs.delete(scheduleKey);
    
    logger.info(`Cancelled schedule for session ${schedule.session_id}`);
  }
}

module.exports = {
  initializeScheduler,
  scheduleSession,
  cancelSchedule
};