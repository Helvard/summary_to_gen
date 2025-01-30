from main import SessionLocal, ConversationSummary
from datetime import datetime

def insert_test_data():
    db = SessionLocal()
    try:
        # Create test records
        test_records = [
            ConversationSummary(
                phone_number="+4512345678",
                summary="Customer called about their internet connection issues. Resolved by resetting the router. Customer was satisfied with the solution.",
                last_call_date=datetime.now()
            ),
            ConversationSummary(
                phone_number="+4587654321",
                summary="Customer inquired about upgrading their service package. Provided information about premium packages. Customer will think about it and call back.",
                last_call_date=datetime.now()
            )
        ]
        
        # Add records to the database
        for record in test_records:
            existing = db.query(ConversationSummary).filter(
                ConversationSummary.phone_number == record.phone_number
            ).first()
            
            if existing:
                print(f"Record for {record.phone_number} already exists, updating...")
                existing.summary = record.summary
                existing.last_call_date = record.last_call_date
            else:
                print(f"Adding new record for {record.phone_number}")
                db.add(record)
        
        # Commit the changes
        db.commit()
        print("Test data inserted successfully!")
        
    except Exception as e:
        print(f"Error inserting test data: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    insert_test_data()
