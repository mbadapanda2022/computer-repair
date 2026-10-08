#!/usr/bin/env python
"""
Database Backup & Restore Utility
=================================
Works with both SQLite and PostgreSQL.
Usage:
    python backup_db.py backup [output_file]
    python backup_db.py restore [backup_file]
"""
import json
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backup_restore_common import backup_all_data, restore_from_json  # noqa: E402


def backup_database(output_file=None):
    """Create a full database backup."""
    if not output_file:
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = f'backup_{timestamp}.json'

    print(f"-> Creating backup: {output_file}")
    backup_data = backup_all_data()

    total = backup_data['metadata']['total_records']
    backup_data['metadata']['total_records'] = total

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(backup_data, f, indent=2, default=str)

    size = os.path.getsize(output_file)
    print(f"[OK] Backup saved: {output_file} ({size:,} bytes, {total} records)")
    return output_file


def restore_database(backup_file):
    """Restore database from backup file."""
    if not os.path.exists(backup_file):
        print(f"[FAIL] Backup file not found: {backup_file}")
        return False

    print(f"-> Restoring from: {backup_file}")

    with open(backup_file, 'r', encoding='utf-8') as f:
        backup_data = json.load(f)

    restore_from_json(backup_data)
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python backup_db.py backup [output_file]")
        print("  python backup_db.py restore [backup_file]")
        sys.exit(1)

    action = sys.argv[1]
    file_arg = sys.argv[2] if len(sys.argv) > 2 else None

    if action == 'backup':
        backup_database(file_arg)
    elif action == 'restore':
        restore_database(file_arg)
    else:
        print(f"Unknown action: {action}")
        print("Usage:")
        print("  python backup_db.py backup [output_file]")
        print("  python backup_db.py restore [backup_file]")
        sys.exit(1)