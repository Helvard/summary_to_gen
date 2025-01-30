from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import requests
import base64
import json
import asyncio
import aiohttp
from typing import Optional, List, Dict

# Set up logging
def setup_logging():
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)
    
    # Create logger
    logger = logging.getLogger('genesys_integration')
    logger.setLevel(logging.DEBUG)
    
    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Create file handler (with rotation)
    file_handler = RotatingFileHandler(
        'logs/genesys_integration.log',
        maxBytes=1024*1024,  # 1MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Initialize logging
logger = setup_logging()

# Load environment variables
load_dotenv()

class GenesysAuth:
    def __init__(self):
        self.client_id = os.getenv("CLIENT_ID")
        self.client_secret = os.getenv("CLIENT_SECRET")
        self.environment = os.getenv("ENVIRONMENT")
        self.auth_url = f"https://login.{self.environment}/oauth/token"
        self.api_url = f"https://api.{self.environment}"
        self._access_token: Optional[str] = None
        self.last_check_time = datetime.utcnow() - timedelta(minutes=5)
        logger.info("GenesysAuth initialized with environment: %s", self.environment)

    async def get_access_token(self) -> str:
        try:
            if not self._access_token:
                logger.debug("Getting new access token")
                auth_string = f"{self.client_id}:{self.client_secret}"
                auth_header = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

                headers = {
                    'Authorization': f'Basic {auth_header}',
                    'Content-Type': 'application/x-www-form-urlencoded'
                }
                
                data = {'grant_type': 'client_credentials'}

                async with aiohttp.ClientSession() as session:
                    async with session.post(self.auth_url, headers=headers, data=data) as response:
                        if response.status == 200:
                            result = await response.json()
                            self._access_token = result['access_token']
                            logger.info("Successfully obtained new access token")
                        else:
                            error_text = await response.text()
                            logger.error("Failed to get access token. Status: %d, Response: %s", 
                                       response.status, error_text)
                            raise HTTPException(
                                status_code=500, 
                                detail=f"Failed to authenticate with Genesys: {error_text}"
                            )

            return self._access_token
        except Exception as e:
            logger.error("Error in get_access_token: %s", str(e), exc_info=True)
            raise

    async def get_recent_conversations(self) -> List[Dict]:
        try:
            access_token = await self.get_access_token()
            current_time = datetime.utcnow()
            
            interval_start = self.last_check_time.isoformat() + 'Z'
            interval_end = current_time.isoformat() + 'Z'
            
            logger.debug("Fetching conversations from %s to %s", interval_start, interval_end)
            
            url = f"{self.api_url}/api/v2/analytics/conversations/details/query"
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            data = {
                "interval": f"{interval_start}/{interval_end}",
                "order": "asc",
                "orderBy": "conversationStart",
                "paging": {
                    "pageSize": 25,
                    "pageNumber": 1
                }
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status == 200:
                        result = await response.json()
                        self.last_check_time = current_time
                        conversations = result.get('conversations', [])
                        logger.info("Retrieved %d conversations", len(conversations))
                        return conversations
                    else:
                        error_text = await response.text()
                        logger.error("Failed to get conversations. Status: %d, Response: %s",
                                   response.status, error_text)
                        return []
                        
        except Exception as e:
            logger.error("Error getting conversations: %s", str(e), exc_info=True)
            return []

class ConversationSummary:
    __tablename__ = "conversation_summaries"
    phone_number = Column(String, primary_key=True)
    last_conversation_id = Column(String)
    last_conversation_time = Column(DateTime, default=datetime.utcnow)
    summary = Column(String)

# Initialize database
DATABASE_URL = "sqlite:///./conversations.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Create tables
Base.metadata.create_all(bind=engine)

# Initialize Genesys client
genesys_auth = GenesysAuth()

async def process_conversation(conversation: dict):
    try:
        participants = conversation.get('participants', [])
        customer = next((p for p in participants if p.get('purpose') == 'customer'), None)
        
        if customer:
            phone_number = customer.get('ani') or customer.get('phoneNumber')
            conversation_id = conversation.get('conversationId')
            
            if phone_number and conversation_id:
                logger.debug("Processing conversation %s for phone number %s", 
                           conversation_id, phone_number)
                
                db = SessionLocal()
                try:
                    summary = db.query(ConversationSummary).filter_by(phone_number=phone_number).first()
                    if not summary:
                        logger.info("Creating new summary for %s", phone_number)
                        summary = ConversationSummary(
                            phone_number=phone_number,
                            summary="Previous interactions: None"
                        )
                        db.add(summary)
                    
                    summary.last_conversation_id = conversation_id
                    summary.last_conversation_time = datetime.utcnow()
                    
                    db.commit()
                    logger.info("Updated summary for %s", phone_number)
                finally:
                    db.close()
    except Exception as e:
        logger.error("Error processing conversation: %s", str(e), exc_info=True)

async def poll_conversations():
    logger.info("Starting conversation polling service")
    while True:
        try:
            conversations = await genesys_auth.get_recent_conversations()
            for conversation in conversations:
                await process_conversation(conversation)
            
            logger.debug("Waiting 30 seconds before next poll")
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error("Error in poll_conversations: %s", str(e), exc_info=True)
            logger.info("Retrying in 5 seconds...")
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start polling task
    task = asyncio.create_task(poll_conversations())
    yield
    # Cancel polling task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CallNotification(BaseModel):
    phone_number: str

@app.post("/call-notification")
async def handle_call_notification(notification: CallNotification):
    db = SessionLocal()
    try:
        summary = db.query(ConversationSummary).filter_by(phone_number=notification.phone_number).first()
        if summary:
            return {"summary": summary.summary}
        return {"summary": "No previous interactions found"}
    finally:
        db.close()

# Add a new endpoint to view all summaries for testing
@app.get("/summaries")
async def get_all_summaries():
    db = SessionLocal()
    try:
        summaries = db.query(ConversationSummary).all()
        return [{"phone_number": s.phone_number, "summary": s.summary, "last_conversation_time": s.last_conversation_time} for s in summaries]
    finally:
        db.close()

@app.get("/conversations/recent")
async def get_recent_conversations():
    """Get the most recently retrieved conversations"""
    try:
        conversations = await genesys_auth.get_recent_conversations()
        formatted_conversations = []
        
        for conv in conversations:
            # Extract customer information
            participants = conv.get('participants', [])
            customer = next((p for p in participants if p.get('purpose') == 'customer'), None)
            
            formatted_conv = {
                'conversation_id': conv.get('conversationId'),
                'start_time': conv.get('conversationStart'),
                'end_time': conv.get('conversationEnd'),
                'customer': {
                    'phone_number': customer.get('ani') if customer else None,
                    'name': customer.get('name') if customer else None,
                } if customer else None,
                'duration': conv.get('conversationDuration'),
                'queue': conv.get('queueName'),
                'direction': conv.get('direction'),
                'status': conv.get('status')
            }
            formatted_conversations.append(formatted_conv)
            
        logger.info(f"Retrieved {len(formatted_conversations)} conversations")
        return {
            "total": len(formatted_conversations),
            "conversations": formatted_conversations
        }
    except Exception as e:
        logger.error(f"Error retrieving conversations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/conversations/{conversation_id}")
async def get_conversation_details(conversation_id: str):
    """Get detailed information about a specific conversation"""
    try:
        access_token = await genesys_auth.get_access_token()
        url = f"{genesys_auth.api_url}/api/v2/analytics/conversations/{conversation_id}/details"
        
        async with aiohttp.ClientSession() as session:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    conversation = await response.json()
                    logger.info(f"Retrieved details for conversation {conversation_id}")
                    return conversation
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to get conversation details. Status: {response.status}, Response: {error_text}")
                    raise HTTPException(status_code=response.status, detail=error_text)
                    
    except Exception as e:
        logger.error(f"Error retrieving conversation details: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
