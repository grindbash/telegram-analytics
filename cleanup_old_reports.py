import os
from supabase import create_client
from datetime import datetime, timedelta

supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

def cleanup():
    cutoff_date = (datetime.now() - timedelta(days=30)).isoformat()
    supabase.table('ai_reports').delete().lt('created_at', cutoff_date).execute()

if __name__ == '__main__':
    cleanup()