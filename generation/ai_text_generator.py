"""
AI-powered text generation for viral content.
Replaces template-based generation with creative, trend-aware scripts using GPT-4/Claude.

This is the CRITICAL component that transforms generic templates into viral content.
"""

import os
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

# Try to import AI clients
try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logger.warning("OpenAI not available. Install with: pip install openai")

try:
    from anthropic import Anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning("Anthropic not available. Install with: pip install anthropic")


@dataclass
class ScriptGenerationContext:
    """Context for AI script generation"""
    niche: str
    trend_topic: str
    target_platform: str
    emotion: str
    hook_type: str
    duration_seconds: int
    format_archetype: str
    viral_examples: List[str]
    previous_segments: List[str] = None


class AITextGenerator:
    """
    Generates creative, viral-optimized scripts using AI.
    
    This replaces the template-based generation in script_generator.py
    with actual AI that can create unique, trend-aware content.
    """
    
    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4-turbo-preview",
        api_key: Optional[str] = None
    ):
        """
        Initialize AI text generator.
        
        Args:
            provider: "openai" or "anthropic"
            model: Model name (e.g., "gpt-4-turbo-preview", "claude-3-opus-20240229")
            api_key: API key (if None, reads from environment)
        """
        self.provider = provider
        self.model = model
        
        if provider == "openai":
            if not OPENAI_AVAILABLE:
                raise ImportError("OpenAI not available. Install: pip install openai")
            self.client = openai.OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
            if not self.client.api_key:
                raise ValueError("OPENAI_API_KEY not set in environment")
                
        elif provider == "anthropic":
            if not ANTHROPIC_AVAILABLE:
                raise ImportError("Anthropic not available. Install: pip install anthropic")
            self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
            if not self.client.api_key:
                raise ValueError("ANTHROPIC_API_KEY not set in environment")
        else:
            raise ValueError(f"Unknown provider: {provider}")
    
    def generate_hook(
        self,
        topic: str,
        niche: str,
        emotion: str,
        platform: str = "tiktok",
        viral_examples: Optional[List[str]] = None,
        max_length: int = 15  # seconds
    ) -> str:
        """
        Generate viral hook (first 3-5 seconds) using AI.
        
        This is the MOST CRITICAL part - the hook determines if people watch.
        """
        
        viral_examples_text = ""
        if viral_examples:
            viral_examples_text = "\n\nEXAMPLES OF VIRAL HOOKS IN THIS NICHE:\n" + "\n".join(
                f"- {ex}" for ex in viral_examples[:5]
            )
        
        prompt = f"""You are an expert at creating viral video hooks that get millions of views.

NICHE: {niche}
TOPIC: {topic}
EMOTION: {emotion}
PLATFORM: {platform}
MAX LENGTH: {max_length} seconds (approximately 20-30 words)

{viral_examples_text}

Create a hook (first 3-5 seconds) that:
1. Creates immediate curiosity or surprise
2. Uses platform-appropriate language ({platform} style)
3. Promises value or reveals something unexpected
4. Is specific and concrete (not generic)
5. Triggers the {emotion} emotion
6. Makes people want to keep watching

Requirements:
- Natural, conversational tone
- No filler words
- Start with impact
- Be specific (use numbers, names, or concrete details)
- Create a "wait, what?" moment

Hook (3-5 seconds, natural speech, {max_length} words max):"""

        try:
            if self.provider == "openai":
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a viral content expert who creates hooks that get millions of views. Be creative, specific, and attention-grabbing."
                        },
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.8,  # Creative but not random
                    max_tokens=100,
                    top_p=0.9
                )
                hook = response.choices[0].message.content.strip()
                
            else:  # anthropic
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=100,
                    temperature=0.8,
                    messages=[{"role": "user", "content": prompt}]
                )
                hook = response.content[0].text.strip()
            
            # Clean up hook
            hook = self._clean_hook(hook)
            logger.info(f"Generated hook: {hook[:50]}...")
            return hook
            
        except Exception as e:
            logger.error(f"Error generating hook: {e}")
            # Fallback to simple template
            return f"This {topic} trick will blow your mind..."
    
    def generate_segment(
        self,
        segment_role: str,
        topic: str,
        previous_text: str,
        emotion: str,
        target_words: int,
        niche: str,
        platform: str,
        viral_patterns: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Generate full segment text using AI.
        
        Args:
            segment_role: "intro", "body", "climax", "cta", "outro"
            topic: Main topic
            previous_text: Previous segment text for context
            emotion: Target emotion
            target_words: Target word count
            niche: Content niche
            platform: Target platform
            viral_patterns: Patterns from successful videos
        """
        
        patterns_text = ""
        if viral_patterns:
            patterns_text = f"\n\nVIRAL PATTERNS TO USE:\n{viral_patterns}"
        
        prompt = f"""Generate a {segment_role} segment for a viral {niche} video on {platform}.

TOPIC: {topic}
PREVIOUS CONTEXT: {previous_text}
EMOTION: {emotion}
TARGET WORDS: {target_words}
{patterns_text}

Requirements:
- Natural, conversational tone (like talking to a friend)
- Platform-optimized pacing ({platform} style)
- Include specific examples, data, or stories
- Build on previous context naturally
- Maintain {emotion} emotional tone throughout
- End with forward momentum (keep viewer engaged)
- Use short sentences for {platform}
- Be authentic, not salesy

Segment text ({target_words} words, {platform} style):"""

        try:
            if self.provider == "openai":
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": f"You are a viral {niche} content creator on {platform}. Create engaging, authentic content."
                        },
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7,
                    max_tokens=target_words * 2,  # Allow some flexibility
                    top_p=0.9
                )
                text = response.choices[0].message.content.strip()
            else:  # anthropic
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=target_words * 2,
                    temperature=0.7,
                    messages=[{"role": "user", "content": prompt}]
                )
                text = response.content[0].text.strip()
            
            # Adjust length if needed
            text = self._adjust_length(text, target_words)
            return text
            
        except Exception as e:
            logger.error(f"Error generating segment: {e}")
            return f"Let's dive into {topic}..."
    
    def generate_full_script(
        self,
        storyboard: Dict[str, Any],
        trend_data: Dict[str, Any],
        viral_examples: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """
        Generate complete script using AI.
        
        This replaces the template-based generation entirely.
        """
        
        niche = storyboard.get('niche', 'general')
        platform = storyboard.get('platform', 'tiktok')
        topic = trend_data.get('topic', storyboard.get('topic', 'content'))
        
        script = {}
        previous_text = ""
        
        # Generate hook first (most critical)
        hook = self.generate_hook(
            topic=topic,
            niche=niche,
            emotion=storyboard.get('target_emotion', 'curiosity'),
            platform=platform,
            viral_examples=viral_examples
        )
        script['hook'] = hook
        previous_text = hook
        
        # Generate body segments
        for segment in storyboard.get('segments', []):
            segment_id = segment.get('segment_id', f"segment_{len(script)}")
            role = segment.get('role', 'body')
            
            # Calculate target words from duration
            duration = segment.get('end', 0) - segment.get('start', 0)
            target_words = int((duration / 60) * 150)  # 150 WPM
            
            text = self.generate_segment(
                segment_role=role,
                topic=topic,
                previous_text=previous_text,
                emotion=segment.get('emotion', 'excite'),
                target_words=max(target_words, 20),  # Minimum 20 words
                niche=niche,
                platform=platform,
                viral_patterns=trend_data.get('viral_patterns')
            )
            
            script[segment_id] = text
            previous_text = text
        
        return script
    
    def _clean_hook(self, hook: str) -> str:
        """Clean and format hook text"""
        # Remove quotes if wrapped
        hook = hook.strip('"\'')
        
        # Remove "Hook:" prefix if present
        if hook.lower().startswith('hook:'):
            hook = hook[5:].strip()
        
        # Ensure it ends with punctuation
        if not hook[-1] in '.!?':
            hook += '!'
        
        return hook
    
    def _adjust_length(self, text: str, target_words: int) -> str:
        """Adjust text length to match target word count"""
        words = text.split()
        current_words = len(words)
        
        if current_words > target_words * 1.2:
            # Too long - truncate
            words = words[:int(target_words * 1.1)]
            text = ' '.join(words)
        elif current_words < target_words * 0.8:
            # Too short - but keep as is (AI might have good reason)
            pass
        
        return text


# Example usage
if __name__ == "__main__":
    # Initialize generator
    generator = AITextGenerator(
        provider="openai",
        model="gpt-4-turbo-preview"
    )
    
    # Generate hook
    hook = generator.generate_hook(
        topic="finance tips",
        niche="finance",
        emotion="curiosity",
        platform="tiktok",
        viral_examples=[
            "This one finance trick made me $50K in 30 days",
            "I quit my job after learning this investment strategy",
            "Most people don't know this about credit cards"
        ]
    )
    
    print(f"Generated Hook: {hook}")
    
    # Generate full script
    storyboard = {
        'niche': 'finance',
        'platform': 'tiktok',
        'target_emotion': 'curiosity',
        'segments': [
            {'segment_id': 'hook', 'role': 'hook', 'start': 0, 'end': 5, 'emotion': 'curiosity'},
            {'segment_id': 'body1', 'role': 'body', 'start': 5, 'end': 30, 'emotion': 'excite'},
            {'segment_id': 'cta', 'role': 'cta', 'start': 30, 'end': 35, 'emotion': 'urgency'}
        ]
    }
    
    trend_data = {
        'topic': 'passive income ideas',
        'viral_patterns': {
            'use_numbers': True,
            'personal_story': True,
            'specific_examples': True
        }
    }
    
    script = generator.generate_full_script(
        storyboard=storyboard,
        trend_data=trend_data,
        viral_examples=[
            "I made $10K/month with this passive income idea",
            "This side hustle changed my life"
        ]
    )
    
    print("\nGenerated Script:")
    for segment_id, text in script.items():
        print(f"\n{segment_id.upper()}:")
        print(text)
