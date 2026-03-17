from django.db.models import Prefetch, Window, F
from django.db.models.functions import RowNumber


def limit_prefetch(relations_name, queryset, order_by='-created_at',limit=3 , nested_prefetch=None):
            direction = 'asc'
            if(order_by[0] == '-'):
                direction = 'desc'
                order_by= order_by[1:]

            return Prefetch(
                relations_name,
                queryset=queryset.annotate(
                    rn=Window(
                        expression=RowNumber(),
                        partition_by=[F('word_id')],
                        order_by=F(order_by).asc() if direction == 'asc' else F(order_by).desc()
                    )
                )
                .filter(rn__lte=limit)      # LIMIT 2 per word
                .prefetch_related(nested_prefetch)
            
        )