-- Lvco BI 数据库初始化脚本
-- 创建必要的扩展和初始表结构

-- 扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 枚举类型
CREATE TYPE user_role AS ENUM ('admin', 'editor', 'viewer');
CREATE TYPE datasource_type AS ENUM ('csv', 'excel', 'mysql', 'postgresql');
CREATE TYPE datasource_status AS ENUM ('connected', 'disconnected', 'syncing');
CREATE TYPE chart_type_enum AS ENUM ('bar', 'line', 'pie', 'scatter', 'area', 'donut');
CREATE TYPE report_status AS ENUM ('draft', 'published', 'shared');
CREATE TYPE report_source_type AS ENUM ('canvas', 'dashboard');
CREATE TYPE ai_message_role AS ENUM ('user', 'assistant');

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    avatar_url VARCHAR(500),
    role user_role DEFAULT 'editor',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 数据源表
CREATE TABLE IF NOT EXISTS datasources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    source_type datasource_type NOT NULL,
    connection_config JSONB,
    file_path VARCHAR(500),
    schema_meta JSONB,
    status datasource_status DEFAULT 'disconnected',
    size_bytes BIGINT DEFAULT 0,
    row_count INTEGER DEFAULT 0,
    last_synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 画布表
CREATE TABLE IF NOT EXISTS canvases (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,
    table_name VARCHAR(200),
    title VARCHAR(200) NOT NULL,
    blocks JSONB DEFAULT '[]',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 画布块表（可选，如果 blocks 存 JSONB 可不用）
-- 这里用 canvases.blocks JSONB 存储，无需单独表

-- 图表配置表
CREATE TABLE IF NOT EXISTS chart_configs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    chart_type chart_type_enum NOT NULL,
    query_config JSONB NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 仪表盘表
CREATE TABLE IF NOT EXISTS dashboards (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    layout JSONB DEFAULT '[]',
    refresh_interval INTEGER DEFAULT 300,
    is_public BOOLEAN DEFAULT FALSE,
    share_token VARCHAR(100) UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 仪表盘图表关联表
CREATE TABLE IF NOT EXISTS dashboard_charts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dashboard_id UUID NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    chart_config_id UUID NOT NULL REFERENCES chart_configs(id) ON DELETE CASCADE,
    title VARCHAR(200),
    position JSONB DEFAULT '{"x": 0, "y": 0, "w": 1, "h": 1}',
    created_at TIMESTAMP DEFAULT NOW()
);

-- 报表表
CREATE TABLE IF NOT EXISTS reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    source_type report_source_type NOT NULL,
    source_id UUID,
    snapshot_blocks JSONB,
    status report_status DEFAULT 'draft',
    share_token VARCHAR(100) UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- AI 会话表
CREATE TABLE IF NOT EXISTS ai_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model VARCHAR(50) DEFAULT 'gpt-4o',
    title VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
);

-- AI 消息表
CREATE TABLE IF NOT EXISTS ai_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES ai_sessions(id) ON DELETE CASCADE,
    role ai_message_role NOT NULL,
    content TEXT NOT NULL,
    chart_data JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_datasources_user_id ON datasources(user_id);
CREATE INDEX IF NOT EXISTS idx_datasources_status ON datasources(status);
CREATE INDEX IF NOT EXISTS idx_canvases_user_id ON canvases(user_id);
CREATE INDEX IF NOT EXISTS idx_canvases_datasource_id ON canvases(datasource_id);
CREATE INDEX IF NOT EXISTS idx_dashboards_user_id ON dashboards(user_id);
CREATE INDEX IF NOT EXISTS idx_dashboards_share_token ON dashboards(share_token);
CREATE INDEX IF NOT EXISTS idx_dashboard_charts_dashboard_id ON dashboard_charts(dashboard_id);
CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id);
CREATE INDEX IF NOT EXISTS idx_reports_source ON reports(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_user_id ON ai_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_session_id ON ai_messages(session_id);

-- 插入初始数据
INSERT INTO users (email, password_hash, display_name, role) 
VALUES ('demo@lvco.bi', '-- 实际使用 bcrypt hash --', '张明', 'admin')
ON CONFLICT (email) DO NOTHING;
