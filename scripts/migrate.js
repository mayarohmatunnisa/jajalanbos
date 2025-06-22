const fs = require('fs').promises;
const path = require('path');
const bcrypt = require('bcrypt');
const db = require('../config/database');
const logger = require('../utils/logger');

async function migrateFromJSON() {
  try {
    logger.info('Starting migration from JSON files...');

    // Migrate users
    try {
      const usersData = await fs.readFile('/root/StreamHibV2/users.json', 'utf8');
      const users = JSON.parse(usersData);
      
      for (const [email, userData] of Object.entries(users)) {
        // Hash the password if it's not already hashed
        let passwordHash = userData.password;
        if (!passwordHash.startsWith('$2b$')) {
          passwordHash = await bcrypt.hash(passwordHash, 12);
        }

        await db.query(`
          INSERT INTO users (email, password_hash) 
          VALUES ($1, $2) 
          ON CONFLICT (email) DO NOTHING
        `, [email, passwordHash]);
      }
      
      logger.info(`Migrated ${Object.keys(users).length} users`);
    } catch (error) {
      logger.warn('No users.json file found or error reading it:', error.message);
    }

    // Migrate sessions
    try {
      const sessionsData = await fs.readFile('/root/StreamHibV2/sessions.json', 'utf8');
      const sessions = JSON.parse(sessionsData);
      
      for (const [sessionId, sessionData] of Object.entries(sessions)) {
        // First, check if video exists or create it
        let videoId = null;
        if (sessionData.video_file) {
          const videoPath = path.join('videos', sessionData.video_file);
          
          // Check if video already exists
          const existingVideo = await db.query(
            'SELECT id FROM videos WHERE filename = $1',
            [sessionData.video_file]
          );

          if (existingVideo.rows.length > 0) {
            videoId = existingVideo.rows[0].id;
          } else {
            // Create video record
            try {
              const stats = await fs.stat(videoPath);
              const videoResult = await db.query(`
                INSERT INTO videos (filename, original_name, file_path, file_size)
                VALUES ($1, $2, $3, $4)
                RETURNING id
              `, [sessionData.video_file, sessionData.video_file, videoPath, stats.size]);
              
              videoId = videoResult.rows[0].id;
            } catch (videoError) {
              logger.warn(`Failed to create video record for ${sessionData.video_file}:`, videoError.message);
              continue;
            }
          }
        }

        // Create session
        const sessionResult = await db.query(`
          INSERT INTO sessions (video_id, stream_key, platform, status)
          VALUES ($1, $2, $3, $4)
          ON CONFLICT DO NOTHING
          RETURNING id
        `, [videoId, sessionData.stream_key, sessionData.platform || 'youtube', 'inactive']);

        if (sessionResult.rows.length > 0) {
          const newSessionId = sessionResult.rows[0].id;

          // Migrate schedules if any
          if (sessionData.schedule) {
            const schedule = sessionData.schedule;
            const scheduleType = schedule.type || 'one_time';
            
            await db.query(`
              INSERT INTO schedules (session_id, schedule_type, start_datetime, end_datetime, timezone, is_active)
              VALUES ($1, $2, $3, $4, $5, $6)
              ON CONFLICT DO NOTHING
            `, [
              newSessionId,
              scheduleType,
              new Date(schedule.start_time),
              new Date(schedule.end_time),
              schedule.timezone || 'UTC',
              schedule.active !== false
            ]);
          }
        }
      }
      
      logger.info(`Migrated ${Object.keys(sessions).length} sessions`);
    } catch (error) {
      logger.warn('No sessions.json file found or error reading it:', error.message);
    }

    logger.info('Migration completed successfully');

  } catch (error) {
    logger.error('Migration failed:', error);
    throw error;
  }
}

// Run migration if called directly
if (require.main === module) {
  migrateFromJSON()
    .then(() => {
      logger.info('Migration script completed');
      process.exit(0);
    })
    .catch((error) => {
      logger.error('Migration script failed:', error);
      process.exit(1);
    });
}

module.exports = { migrateFromJSON };