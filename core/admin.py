# core/admin.py
from django.contrib import admin

from .models import Example,ExampleTranslate, Collection,CommonPhrase,UserCollection,Language,Quiz,Translate,Word,WordInfo

admin.site.register(Collection)
admin.site.register(CommonPhrase)
admin.site.register(UserCollection)
admin.site.register(Language)
admin.site.register(Example)
admin.site.register(ExampleTranslate)
admin.site.register(Quiz)
admin.site.register(Translate)
# admin.site.register(Word)
admin.site.register(Word)
# class WordConfig(admin.ModelAdmin):
    # list_display = ('user', 'target_type', 'target_id', 'created_at', 'is_success')
    # list_filter = ('value', 'note')  # ✅ cần là tuple, có dấu phẩy
    # search_fields = ('value', 'note')
    # ordering = ('-created_at',)
    # list_per_page = 50
    # autocomplete_fields = ("value",)
admin.site.register(WordInfo)