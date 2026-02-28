"""
Ordering policy provider for replay execution.

This module provides canonical ordering policies that can be configured
externally. The runner delegates all ordering decisions to this module.
"""

from __future__ import annotations

from typing import List, Protocol, Optional


class OrderingPolicy(Protocol):
    """
    Protocol for ordering policy providers.
    
    The runner delegates all ordering decisions to implementations of this protocol.
    """
    
    def canonical_sort(self, items: List[str]) -> List[str]:
        """
        Apply canonical ordering to a list of items.
        
        Args:
            items: List of items to sort
            
        Returns:
            Sorted list in canonical order
        """
        ...


class LexicographicOrderingPolicy:
    """
    Simple lexicographic ordering policy.
    
    No normalization, just pure lexicographic sort.
    """
    
    def canonical_sort(self, items: List[str]) -> List[str]:
        """Sort items lexicographically."""
        return sorted(items)


class VersionAwareOrderingPolicy:
    """
    Ordering policy that normalizes version prefixes.
    
    Handles cases like "v1:entity_123" by removing version prefix before sorting.
    """
    
    def canonical_sort(self, items: List[str]) -> List[str]:
        """
        Sort with version prefix normalization.
        
        Removes version prefixes (e.g., "v1:entity_123" -> "entity_123")
        before sorting, then sorts by normalized form.
        """
        normalized = []
        for item in items:
            # Remove version prefixes if present (e.g., "v1:entity_123" -> "entity_123")
            if ':' in item and item.split(':', 1)[0].startswith('v'):
                normalized.append((item.split(':', 1)[1], item))
            else:
                normalized.append((item, item))
        
        # Sort by normalized form, then by original for stability
        normalized.sort(key=lambda x: (x[0], x[1]))
        return [orig for _, orig in normalized]


__all__ = [
    'OrderingPolicy',
    'LexicographicOrderingPolicy',
    'VersionAwareOrderingPolicy',
]
