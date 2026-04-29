import os
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Get the DB URL from your environment, or fallback to your default
db_uri = os.environ.get('DATABASE_URL', 'postgresql://xmltv:ballzXMLTVballz@192.168.1.198/xmltv')

def clean_database():
    print(f"Target Database: {db_uri}")
    print("WARNING: This will completely wipe all tables, ghost tables, and data!")
    
    confirm = input("Are you absolutely sure you want to proceed? (type 'yes'): ")
    
    if confirm.lower() == 'yes':
        try:
            # Connect directly to Postgres
            conn = psycopg2.connect(db_uri)
            
            # Set autocommit so we can run structural changes like DROP SCHEMA
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            
            # Obliterate the schema and recreate it
            print("Dropping public schema...")
            cur.execute("DROP SCHEMA public CASCADE;")
            
            print("Recreating public schema...")
            cur.execute("CREATE SCHEMA public;")
            cur.execute("GRANT ALL ON SCHEMA public TO public;")
            
            # Close connection
            cur.close()
            conn.close()
            
            print("✅ All tables and dependent objects have been successfully dropped.")
            print("The database is now completely empty. Run app.py to rebuild!")
        except Exception as e:
            print(f"❌ An error occurred: {e}")
    else:
        print("❌ Operation cancelled. Your database was not touched.")

if __name__ == "__main__":
    clean_database()