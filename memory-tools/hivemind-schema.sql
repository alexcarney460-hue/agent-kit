CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT DEFAULT '*',
            channel TEXT DEFAULT 'general',
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'info',
            priority INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP,
            expires_at TIMESTAMP
        );
CREATE TABLE learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            rule TEXT NOT NULL,
            context TEXT,
            source_session TEXT,
            confidence REAL DEFAULT 0.5,
            times_applied INTEGER DEFAULT 0,
            times_helpful INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_applied TIMESTAMP,
            active INTEGER DEFAULT 1
        , protected INTEGER DEFAULT 0, valid_from TEXT, valid_to TEXT, recorded_at TEXT, expired_at TEXT, superseded_by INTEGER);
CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            working_dir TEXT,
            trigger_type TEXT DEFAULT 'manual',
            cron_schedule TEXT,
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 1,
            retry_count INTEGER DEFAULT 0,
            result TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            next_run TIMESTAMP
        );
CREATE TABLE agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT UNIQUE NOT NULL,
            name TEXT,
            callsign TEXT,
            role TEXT,
            rank TEXT DEFAULT 'private',
            rank_level INTEGER DEFAULT 1,
            reports_to TEXT,
            division TEXT DEFAULT 'general',
            specialization TEXT,
            device TEXT,
            status TEXT DEFAULT 'active',
            pid INTEGER,
            working_dir TEXT,
            tasks_completed INTEGER DEFAULT 0,
            tasks_failed INTEGER DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat TIMESTAMP,
            ended_at TIMESTAMP,
            FOREIGN KEY (reports_to) REFERENCES agents(agent_id)
        );
CREATE TABLE chain_of_command (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            division TEXT NOT NULL,
            rank TEXT NOT NULL,
            rank_level INTEGER NOT NULL,
            can_assign_to TEXT,
            can_approve INTEGER DEFAULT 0,
            can_spawn INTEGER DEFAULT 0,
            max_direct_reports INTEGER DEFAULT 5
        );
CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issued_by TEXT NOT NULL,
            assigned_to TEXT NOT NULL,
            order_type TEXT DEFAULT 'task',
            priority TEXT DEFAULT 'normal',
            objective TEXT NOT NULL,
            details TEXT,
            constraints TEXT,
            deadline TIMESTAMP,
            status TEXT DEFAULT 'issued',
            accepted_at TIMESTAMP,
            completed_at TIMESTAMP,
            report TEXT,
            parent_order_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (issued_by) REFERENCES agents(agent_id),
            FOREIGN KEY (assigned_to) REFERENCES agents(agent_id),
            FOREIGN KEY (parent_order_id) REFERENCES orders(id)
        );
CREATE TABLE divisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            mission TEXT,
            commander_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (commander_id) REFERENCES agents(agent_id)
        );
CREATE TABLE session_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            content TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            pattern TEXT DEFAULT '*',
            task_id INTEGER,
            prompt TEXT,
            active INTEGER DEFAULT 1,
            last_triggered TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
CREATE TABLE desktop_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            target TEXT,
            parameters TEXT,
            screenshot_before TEXT,
            screenshot_after TEXT,
            success INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
