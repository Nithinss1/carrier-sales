# app/models.py - Enhanced Enterprise Data Models
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Dict, Any
from datetime import datetime

# Original models (keep existing)
class VerifyResponse(BaseModel):
    mc: str
    eligible: bool
    status: str

class Load(BaseModel):
    load_id: str
    origin: str
    destination: str
    pickup_datetime: str
    delivery_datetime: str
    equipment_type: str
    loadboard_rate: float
    notes: Optional[str] = ""
    weight: Optional[float] = None
    commodity_type: Optional[str] = None
    num_of_pieces: Optional[int] = None
    miles: Optional[int] = None
    dimensions: Optional[str] = None

class SearchRequest(BaseModel):
    origin: Optional[str] = None
    destination: Optional[str] = None
    equipment_type: Optional[str] = None

class Offer(BaseModel):
    load_id: str
    listed_rate: float
    counter_offer: float

class NegotiateResponse(BaseModel):
    accepted: bool
    reason: Optional[str] = None
    agreed_rate: Optional[float] = None
    counter: Optional[float] = None

class PostCallPayload(BaseModel):
    mc: str
    load_id: Optional[str] = None
    final_rate: Optional[float] = None
    outcome: Literal["accepted","declined","no_match","failed_verification","abandoned"]
    sentiment: Literal["positive","neutral","negative"]
    summary: Optional[str] = ""

# Enhanced Enterprise Models

class CarrierIntelligence(BaseModel):
    """Enhanced carrier verification with business intelligence"""
    mc: str
    dot: Optional[str] = None
    eligible: bool
    status: str
    risk_score: int = Field(..., ge=0, le=100, description="Composite risk score (0-100)")
    carrier_tier: Literal["platinum", "gold", "silver", "bronze"]
    historical_loads: int = Field(default=0, description="Previous successful loads")
    lifetime_value: float = Field(default=0, description="Predicted lifetime value score")
    business_recommendation: Literal["approved", "approved_with_monitoring", "manual_review_required", "rejected"]
    verification_timestamp: datetime = Field(default_factory=datetime.now)

class MarketIntelligence(BaseModel):
    """Real-time market intelligence for pricing decisions"""
    average_rate_for_equipment: float
    capacity_tightness: Literal["tight", "balanced", "loose"]
    rate_trend: Literal["increasing", "stable", "decreasing"]
    regional_demand: Literal["high", "balanced", "low"]
    fuel_impact_percentage: Optional[float] = 0.0

class EnhancedLoad(Load):
    """Load with business intelligence enhancements"""
    market_adjusted_rate: float
    rate_premium: float = Field(description="Percentage above/below market rate")
    selling_points: List[str] = []
    urgency_indicator: Literal["critical", "high", "medium", "low"]
    margin_flexibility: float = Field(description="Maximum negotiation flexibility")
    shipper_rating: str = "A"
    payment_terms: str = "Net 30"

class LoadMatchResponse(BaseModel):
    """Intelligent load matching response"""
    total_matches: int
    loads: List[Dict[str, Any]]  # Enhanced load objects
    market_intelligence: MarketIntelligence
    presentation_strategy: str
    upselling_opportunities: Optional[List[str]] = []

class NegotiationStrategy(BaseModel):
    """Advanced negotiation strategy response"""
    action: Literal["accept", "counter_offer", "escalate", "reject_politely", "final_offer_or_escalate"]
    agreed_rate: Optional[float] = None
    counter_rate: Optional[float] = None
    message: str
    confidence_score: int = Field(..., ge=0, le=100)
    business_justification: Optional[str] = None
    next_steps: Optional[List[str]] = []

class CallAnalytics(BaseModel):
    """Comprehensive call analytics for business intelligence"""
    mc: str
    call_duration_seconds: int
    equipment_type: Optional[str] = None
    loads_presented: int = 0
    negotiation_rounds: int = 0
    outcome: Literal["booked", "declined", "callback", "escalated", "abandoned"]
    sentiment: Literal["positive", "neutral", "negative", "frustrated"]
    carrier_satisfaction: Optional[int] = Field(None, ge=1, le=5)
    revenue_generated: Optional[float] = 0.0
    margin_achieved: Optional[float] = None
    competitive_intelligence: Optional[Dict[str, Any]] = {}
    follow_up_actions: List[str] = []

class BusinessMetrics(BaseModel):
    """Executive dashboard metrics"""
    total_calls: int
    qualified_carriers: int
    conversion_rate: float
    revenue_per_call: float
    average_call_duration: str
    cost_per_acquisition: float
    margin_protection_rate: float
    roi_percentage: float

class CarrierProfile(BaseModel):
    """Comprehensive carrier relationship profile"""
    mc: str
    dot: Optional[str] = None
    company_name: str
    risk_score: int
    tier: Literal["platinum", "gold", "silver", "bronze"]
    total_loads: int = 0
    lifetime_revenue: float = 0.0
    on_time_performance: float = 100.0
    payment_history: Literal["excellent", "good", "fair", "poor"] = "good"
    preferred_equipment: List[str] = []
    preferred_lanes: List[str] = []
    communication_style: Literal["formal", "casual", "direct", "relationship_focused"] = "professional"
    last_interaction: Optional[datetime] = None
    notes: Optional[str] = ""

class MarketDataPoint(BaseModel):
    """Market rate and capacity data"""
    equipment_type: str
    origin_region: str
    destination_region: str
    average_rate: float
    rate_range_low: float
    rate_range_high: float
    capacity_ratio: float  # loads/trucks ratio
    trend_direction: Literal["up", "stable", "down"]
    last_updated: datetime = Field(default_factory=datetime.now)

class UpsellOpportunity(BaseModel):
    """Cross-selling and upselling opportunities"""
    opportunity_type: Literal["return_load", "multi_stop", "dedicated_lane", "equipment_upgrade"]
    description: str
    potential_revenue: float
    probability: float = Field(..., ge=0, le=1)
    next_action: str

class SystemHealth(BaseModel):
    """System monitoring and health metrics"""
    status: Literal["healthy", "degraded", "critical"]
    uptime_percentage: float
    api_response_time_ms: float
    fmcsa_api_status: Literal["online", "degraded", "offline"]
    load_database_status: Literal["online", "offline"]
    active_calls: int
    queue_depth: int
    error_rate: float