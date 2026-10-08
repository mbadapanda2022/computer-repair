# Database Backup & Restore System

Professional backup and restore utility for the A1 Computer Solutions application.
Works seamlessly with both **SQLite** (local development) and **PostgreSQL** (Render production).

## Features

- **Cross-database compatible**: Backup from PostgreSQL, restore to SQLite and vice versa
- **JSON format**: Portable, human-readable, no binary dependencies
- **M2M support**: Many-to-many relationships preserved
- **Zero downtime**: Backup while application is running
- **Automatic timestamp**: Default filenames include date/time

## Usage

### Create Backup

```bash
# Auto-generated filename (backup_YYYYMMDD_HHMMSS.json)
python backup_db.py backup

# Custom filename
python backup_db.py backup my_backup.json
```

### Restore Database

```bash
# Restore from backup file
python backup_db.py restore backup_20250101_120000.json
```

## Workflow: Development <-> Production Sync

### Production to Development

1. **On Render (production)**:
   ```bash
   python backup_db.py backup production_backup.json
   ```

2. **Download** the backup file from Render

3. **Locally (development)**:
   ```bash
   python backup_db.py restore production_backup.json
   ```

### Development to Production

1. **Locally (development)**:
   ```bash
   python backup_db.py backup dev_backup.json
   ```

2. **Upload** the backup file to Render

3. **On Render (production)**:
   ```bash
   python backup_db.py restore dev_backup.json
   ```

## Backup File Structure

The backup file is a JSON with this structure:

```json
{
  "metadata": {
    "timestamp": "2025-01-01T12:00:00",
    "database_engine": "postgresql",
    "total_records": 680
  },
  "data": {
    "accounting.Contact": [...],
    "accounting.Invoice": [...],
    ...
  }
}
```

## Important Notes

- Backup file contains **all data** including users, sessions, audit logs
- Restore **clears existing data** before restoring (full replacement)
- For safety, always verify backup file size before restore
- Large databases may take several minutes to backup/restore

## Management Command Alternative

You can also use the Django management command:

```bash
# Backup
python manage.py backup_restore backup --file=backup.json

# Restore
python manage.py backup_restore restore --file=backup.json
```

## File Location

- Backup files are saved in project root by default
- Add `*.json` to `.gitignore` if you don't want to commit backups
- Recommended: Store backups in a dedicated `backups/` directory