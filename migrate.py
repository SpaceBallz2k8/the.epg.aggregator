import os
import psycopg2

# Connection string from environment or default
db_uri = os.environ.get('DATABASE_URL', 'postgresql://xmltv:ballzXMLTVballz@192.168.1.198/xmltv')

def migrate():
    print(f"Targeting Database: {db_uri}")
    try:
        # Connect to the database
        conn = psycopg2.connect(db_uri)
        conn.autocommit = True
        cur = conn.cursor()

        print("Updating schema...")

        # 0. Add BannedIP table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned_ip (
                id SERIAL PRIMARY KEY,
                ip VARCHAR(50) UNIQUE NOT NULL,
                reason VARCHAR(200),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # 0b. Add GroupingJob table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS grouping_job (
                id SERIAL PRIMARY KEY,
                channel_ids JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # 0c. Add ChannelCollection tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_collection (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                description TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS collection_channels (
                collection_id INTEGER REFERENCES channel_collection(id) ON DELETE CASCADE,
                channel_id VARCHAR(100) REFERENCES channel(id) ON DELETE CASCADE,
                PRIMARY KEY (collection_id, channel_id)
            );
        """)
        # 0d. Add Category tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS category (
                id SERIAL PRIMARY KEY,
                name VARCHAR(50) UNIQUE NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_categories (
                channel_id VARCHAR(100) REFERENCES channel(id) ON DELETE CASCADE,
                category_id INTEGER REFERENCES category(id) ON DELETE CASCADE,
                PRIMARY KEY (channel_id, category_id)
            );
        """)
        # 1. Add logo_override to ChannelGroup
        print(" - Checking channel_group table...")
        cur.execute("ALTER TABLE channel_group ADD COLUMN IF NOT EXISTS logo_override TEXT;")

        # 2. Add metadata overrides to Channel (in case they were missing)
        print(" - Checking channel table...")
        cur.execute("ALTER TABLE channel ADD COLUMN IF NOT EXISTS logo_override TEXT;")
        cur.execute("ALTER TABLE channel ADD COLUMN IF NOT EXISTS tvg_id_override VARCHAR(100);")
        cur.execute("ALTER TABLE channel ADD COLUMN IF NOT EXISTS name_override VARCHAR(200);")

        # 3. Ensure indices exist for performance
        print(" - Ensuring indices...")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_name_norm ON channel (name_norm);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_country_id ON channel (country_id);")

        print("✅ Database schema updated successfully!")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Migration failed: {e}")

if __name__ == "__main__":
    migrate()
