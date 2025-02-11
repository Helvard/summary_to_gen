from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import os
import json
import base64
import aiohttp
import logging
import asyncio
from dotenv import load_dotenv
import requests
import logging.handlers
import uvicorn

# Load environment variables
load_dotenv()

# Initialize SQLAlchemy
DATABASE_URL = "sqlite:///./conversations.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    file_handler = logging.handlers.RotatingFileHandler(
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

class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    phone_number = Column(String, primary_key=True)
    last_conversation_id = Column(String)
    last_conversation_time = Column(DateTime, default=datetime.utcnow)
    summary = Column(String)

class ConversationDetail(Base):
    __tablename__ = "conversation_details"
    conversation_id = Column(String, primary_key=True)
    phone_number = Column(String, index=True)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    duration = Column(String)
    direction = Column(String)
    status = Column(String)
    queue_name = Column(String)
    full_details = Column(String)  # JSON string of all conversation details
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(bind=engine)

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
                    # Check if we already have this conversation
                    existing_conversation = db.query(ConversationDetail).filter_by(
                        conversation_id=conversation_id
                    ).first()
                    
                    if not existing_conversation:
                        logger.info("Saving new conversation %s", conversation_id)
                        
                        try:
                            # Parse dates
                            start_time = None
                            if conversation.get('conversationStart'):
                                start_time = datetime.fromisoformat(
                                    conversation['conversationStart'].replace('Z', '+00:00')
                                )
                            
                            end_time = None
                            if conversation.get('conversationEnd'):
                                end_time = datetime.fromisoformat(
                                    conversation['conversationEnd'].replace('Z', '+00:00')
                                )
                            
                            # Save full conversation details
                            conversation_detail = ConversationDetail(
                                conversation_id=conversation_id,
                                phone_number=phone_number,
                                start_time=start_time,
                                end_time=end_time,
                                duration=conversation.get('conversationDuration'),
                                direction=conversation.get('direction'),
                                status=conversation.get('status'),
                                queue_name=conversation.get('queueName'),
                                full_details=json.dumps(conversation)
                            )
                            db.add(conversation_detail)
                            
                            # Update summary
                            summary = db.query(ConversationSummary).filter_by(phone_number=phone_number).first()
                            if not summary:
                                summary = ConversationSummary(
                                    phone_number=phone_number,
                                    summary="Previous interactions: None"
                                )
                                db.add(summary)
                            
                            summary.last_conversation_id = conversation_id
                            summary.last_conversation_time = datetime.utcnow()
                            
                            db.commit()
                            logger.info("Saved new conversation and updated summary for %s", phone_number)
                        except (ValueError, json.JSONDecodeError) as e:
                            logger.error(f"Error processing conversation data: {str(e)}")
                            db.rollback()
                    else:
                        logger.debug("Conversation %s already exists, skipping", conversation_id)
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
    """Get the most recently retrieved conversations with all available details"""
    try:
        conversations = await genesys_auth.get_recent_conversations()
        formatted_conversations = []
        
        for conv in conversations:
            # Extract all participant information
            participants = conv.get('participants', [])
            participants_info = []
            
            for participant in participants:
                participant_info = {
                    'id': participant.get('participantId'),
                    'purpose': participant.get('purpose'),
                    'name': participant.get('name'),
                    'phone_number': participant.get('ani'),
                    'sessions': participant.get('sessions', []),
                    'attributes': participant.get('attributes', {}),
                    'user_id': participant.get('userId'),
                    'external_contact_id': participant.get('externalContactId'),
                    'team_id': participant.get('teamId'),
                    'queue_id': participant.get('queueId')
                }
                participants_info.append(participant_info)

            # Format conversation with all available data
            formatted_conv = {
                'conversation_id': conv.get('conversationId'),
                'start_time': conv.get('conversationStart'),
                'end_time': conv.get('conversationEnd'),
                'duration': conv.get('conversationDuration'),
                'participants': participants_info,
                'division_ids': conv.get('divisionIds', []),
                'queue': {
                    'name': conv.get('queueName'),
                    'id': conv.get('queueId')
                },
                'metrics': {
                    'hold_time': conv.get('metrics', {}).get('holdDuration'),
                    'talk_time': conv.get('metrics', {}).get('talkDuration'),
                    'acw_time': conv.get('metrics', {}).get('acwDuration'),
                    'handle_time': conv.get('metrics', {}).get('handleDuration')
                },
                'direction': conv.get('direction'),
                'status': conv.get('status'),
                'segment_count': conv.get('segmentCount'),
                'evaluations': conv.get('evaluations', []),
                'recordings': conv.get('recordings', []),
                'flows': conv.get('flows', []),
                'wrapup': {
                    'codes': conv.get('wrapupCodes', []),
                    'notes': conv.get('wrapupNotes', [])
                },
                'surveys': conv.get('surveys', []),
                'script_ids': conv.get('scriptIds', []),
                'media_stats': conv.get('mediaStats', {}),
                'journey': conv.get('journey', {}),
                'flagged_reason': conv.get('flaggedReason'),
                'peer_ids': conv.get('peerIds', []),
                'purposes': conv.get('purposes', []),
                'conference_ids': conv.get('conferenceIds', []),
                'transfer_info': conv.get('transferInfo', {}),
                'external_tags': conv.get('externalTags', [])
            }
            formatted_conversations.append(formatted_conv)
            
        logger.info(f"Retrieved {len(formatted_conversations)} conversations with full details")
        return {
            "total": len(formatted_conversations),
            "conversations": formatted_conversations
        }
    except Exception as e:
        logger.error(f"Error retrieving conversations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/conversations/{conversation_id}")
async def get_conversation_details(conversation_id: str):
    """Get comprehensive details about a specific conversation including transcripts and analytics"""
    try:
        # Validate UUID format
        if len(conversation_id) != 36:
            raise HTTPException(
                status_code=400,
                detail="Invalid conversation ID format. Expected a 36-character UUID."
            )

        access_token = await genesys_auth.get_access_token()
        base_url = f"{genesys_auth.api_url}/api/v2"
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        async with aiohttp.ClientSession() as session:
            # First check if we have it in our database
            db = SessionLocal()
            try:
                saved_conversation = db.query(ConversationDetail).filter_by(
                    conversation_id=conversation_id
                ).first()
                
                if saved_conversation:
                    return {
                        "source": "database",
                        "conversation": {
                            "conversation_id": saved_conversation.conversation_id,
                            "phone_number": saved_conversation.phone_number,
                            "start_time": saved_conversation.start_time.isoformat() if saved_conversation.start_time else None,
                            "end_time": saved_conversation.end_time.isoformat() if saved_conversation.end_time else None,
                            "duration": saved_conversation.duration,
                            "direction": saved_conversation.direction,
                            "status": saved_conversation.status,
                            "queue_name": saved_conversation.queue_name,
                            "details": json.loads(saved_conversation.full_details) if saved_conversation.full_details else {}
                        }
                    }
            finally:
                db.close()

            # If not in database, try to get from Genesys API
            details_url = f"{base_url}/analytics/conversations/{conversation_id}/details"
            async with session.get(details_url, headers=headers) as response:
                if response.status == 404:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Conversation {conversation_id} not found"
                    )
                elif response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Failed to get conversation details. Status: {response.status}, Response: {error_text}")
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to get conversation details: {error_text}"
                    )
                
                conversation = await response.json()

            # Get additional data if available
            additional_data = {}

            try:
                # Get transcription if available
                transcript_url = f"{base_url}/conversations/{conversation_id}/communications/messages"
                async with session.get(transcript_url, headers=headers) as response:
                    if response.status == 200:
                        additional_data['transcript'] = await response.json()
            except Exception as e:
                logger.warning(f"Failed to get transcript: {str(e)}")

            try:
                # Get quality evaluations
                eval_url = f"{base_url}/quality/conversations/{conversation_id}/evaluations"
                async with session.get(eval_url, headers=headers) as response:
                    if response.status == 200:
                        additional_data['evaluations'] = await response.json()
            except Exception as e:
                logger.warning(f"Failed to get evaluations: {str(e)}")

            try:
                # Get survey responses
                survey_url = f"{base_url}/conversations/{conversation_id}/surveys"
                async with session.get(survey_url, headers=headers) as response:
                    if response.status == 200:
                        additional_data['surveys'] = await response.json()
            except Exception as e:
                logger.warning(f"Failed to get surveys: {str(e)}")

            return {
                "source": "api",
                "conversation": {
                    **conversation,
                    "additional_data": additional_data
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving conversation details: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/conversations/saved")
async def get_saved_conversations(
    phone_number: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    """Get saved conversations with optional filtering"""
    try:
        db = SessionLocal()
        query = db.query(ConversationDetail)
        
        if phone_number:
            query = query.filter(ConversationDetail.phone_number == phone_number)
        
        if start_date:
            try:
                start = datetime.fromisoformat(start_date)
                query = query.filter(ConversationDetail.start_time >= start)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid start_date format: {str(e)}")
            
        if end_date:
            try:
                end = datetime.fromisoformat(end_date)
                query = query.filter(ConversationDetail.start_time <= end)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid end_date format: {str(e)}")
            
        conversations = query.order_by(ConversationDetail.start_time.desc()).all()
        
        result = []
        for conv in conversations:
            try:
                details = json.loads(conv.full_details) if conv.full_details else {}
                result.append({
                    "conversation_id": conv.conversation_id,
                    "phone_number": conv.phone_number,
                    "start_time": conv.start_time.isoformat() if conv.start_time else None,
                    "end_time": conv.end_time.isoformat() if conv.end_time else None,
                    "duration": conv.duration,
                    "direction": conv.direction,
                    "status": conv.status,
                    "queue_name": conv.queue_name,
                    "details": details
                })
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON for conversation {conv.conversation_id}")
                continue
            except Exception as e:
                logger.error(f"Error processing conversation {conv.conversation_id}: {str(e)}")
                continue
        
        return {
            "total": len(result),
            "conversations": result
        }
    except Exception as e:
        logger.error(f"Error retrieving saved conversations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
