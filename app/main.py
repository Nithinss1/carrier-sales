# Add this import at the top with your other imports
import re
from typing import Union

# Add this new model for webhook payloads
class WebhookPayload(BaseModel):
    message: str
    caller_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

# Add this helper function for MC number extraction
def extract_mc_number(text: str) -> Optional[str]:
    """Extract MC number from natural language text"""
    if not text:
        return None
    
    # Common patterns for MC numbers
    patterns = [
        r'(?:mc|MC)\s*(?:number|#)?\s*(?:is\s+)?(\d{4,7})',  # "MC number is 123456" or "MC 123456"
        r'(?:my\s+)?(?:mc|MC)\s+(?:is\s+)?(\d{4,7})',       # "my MC is 123456"
        r'(\d{4,7})\s*(?:mc|MC)',                           # "123456 MC"
        r'motor\s+carrier\s+(?:number\s+)?(\d{4,7})',       # "motor carrier number 123456"
        r'(?:^|\s)(\d{6,7})(?:\s|$)',                       # standalone 6-7 digit number
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            mc_num = match.group(1).strip()
            # Validate it's a reasonable MC number (4-7 digits)
            if 4 <= len(mc_num) <= 7 and mc_num.isdigit():
                return mc_num
    
    return None

# Add this new endpoint to handle webhook from HappyRobot
@app.post("/api/v1/webhook/carrier_call")
async def handle_carrier_call_webhook(
    payload: WebhookPayload,
    x_api_key: Optional[str] = Header(None)
):
    """Handle incoming webhook from HappyRobot platform with natural language"""
    require_key(x_api_key)
    
    try:
        # Extract MC number from the message
        mc_number = extract_mc_number(payload.message)
        
        if not mc_number:
            return {
                "status": "error",
                "message": "Could not extract MC number from message",
                "response": "I'm sorry, I couldn't find your MC number. Could you please say it again? For example, 'My MC number is 123456'",
                "action": "request_clarification"
            }
        
        # Verify the carrier using existing logic
        verification_request = MCVerificationRequest(mc=mc_number)
        carrier_intel = await verify_carrier_enhanced(
            request=verification_request,
            x_api_key=x_api_key
        )
        
        # Determine next action based on verification
        if not carrier_intel.eligible:
            return {
                "status": "not_eligible",
                "mc_number": mc_number,
                "message": f"Thank you for calling. I see your MC number {mc_number}, but unfortunately we're not able to work with carriers that have an out-of-service status. Please contact us again once your authority is active.",
                "action": "end_call"
            }
        
        # If eligible, prepare for load matching
        response_message = f"Great! I have your MC number {mc_number} and you're approved to work with us. "
        
        if carrier_intel.carrier_tier == "platinum":
            response_message += "I see you're one of our platinum carriers - let me find you some premium loads. "
        elif carrier_intel.carrier_tier == "gold":
            response_message += "As a gold-tier carrier, I have some excellent opportunities for you. "
        
        response_message += "What type of equipment are you running and where are you looking to pick up?"
        
        return {
            "status": "verified",
            "mc_number": mc_number,
            "carrier_tier": carrier_intel.carrier_tier,
            "risk_score": carrier_intel.risk_score,
            "message": response_message,
            "action": "collect_equipment_info",
            "next_step": "load_matching"
        }
        
    except Exception as e:
        logger.error(f"Webhook processing failed: {str(e)}")
        return {
            "status": "error",
            "message": "I'm experiencing technical difficulties. Let me transfer you to one of our representatives.",
            "action": "transfer_to_human"
        }

# Add endpoint for equipment/location collection
@app.post("/api/v1/webhook/equipment_info")
async def handle_equipment_info(
    payload: WebhookPayload,
    mc_number: str,
    x_api_key: Optional[str] = Header(None)
):
    """Handle equipment and location information from carrier"""
    require_key(x_api_key)
    
    try:
        # Extract equipment type and location from message
        equipment_type = extract_equipment_type(payload.message)
        origin = extract_location(payload.message)
        
        if not equipment_type:
            return {
                "status": "need_equipment",
                "message": "What type of equipment are you running? Dry van, reefer, or flatbed?",
                "action": "request_equipment_type"
            }
        
        if not origin:
            return {
                "status": "need_location", 
                "message": f"Perfect, I have {equipment_type}. Where are you looking to pick up from?",
                "action": "request_pickup_location"
            }
        
        # Search for matching loads
        search_request = SearchRequest(
            equipment_type=equipment_type,
            origin=origin
        )
        
        load_matches = match_loads_intelligent(
            request=search_request,
            carrier_mc=mc_number,
            x_api_key=x_api_key
        )
        
        if load_matches.total_matches == 0:
            return {
                "status": "no_matches",
                "message": f"I don't have any {equipment_type.lower()} loads out of {origin} right now, but let me check some nearby areas. Can you run within 50 miles?",
                "action": "expand_search"
            }
        
        # Present best load
        best_load = load_matches.loads[0]
        rate = best_load.get("market_adjusted_rate", best_load.get("loadboard_rate"))
        
        response_message = f"I have a great {equipment_type.lower()} load for you! "
        response_message += f"Picking up in {best_load.get('origin')} going to {best_load.get('destination')} "
        response_message += f"for ${rate}. The load picks up {best_load.get('pickup_datetime', 'soon')}. "
        response_message += "Are you interested?"
        
        return {
            "status": "load_presented",
            "load_id": best_load.get("load_id"),
            "rate": rate,
            "message": response_message,
            "action": "await_response",
            "load_details": best_load
        }
        
    except Exception as e:
        logger.error(f"Equipment info processing failed: {str(e)}")
        return {
            "status": "error",
            "message": "Let me get a live agent to help you with this.",
            "action": "transfer_to_human"
        }

def extract_equipment_type(text: str) -> Optional[str]:
    """Extract equipment type from natural language"""
    text_lower = text.lower()
    
    if any(word in text_lower for word in ['dry', 'van', 'dry van', 'dryvan']):
        return "Dry Van"
    elif any(word in text_lower for word in ['reefer', 'refrigerated', 'temp control']):
        return "Reefer"
    elif any(word in text_lower for word in ['flatbed', 'flat bed', 'flat']):
        return "Flatbed"
    
    return None

def extract_location(text: str) -> Optional[str]:
    """Extract location from natural language"""
    # This is a simplified version - you might want to use a more sophisticated
    # location extraction service or maintain a database of cities/states
    
    # Common patterns for locations
    patterns = [
        r'(?:from|in|at|around)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s*,\s*[A-Z]{2})?)',
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2})',
        r'([A-Z][a-z]+\s*,\s*[A-Z]{2})',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            return matches[0].strip()
    
    return None