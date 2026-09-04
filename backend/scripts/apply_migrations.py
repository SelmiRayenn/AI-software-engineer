from app.db.migrations import upgrade_database

if __name__ == "__main__":
    upgrade_database()
    print("Database upgraded to the latest migration.")
