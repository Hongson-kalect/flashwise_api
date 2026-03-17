import fugashi
import re

class RubyGenerator:
    """Generate structured ruby format: (kanji-reading)kana"""
    
    def __init__(self):
        self.tagger = fugashi.Tagger()
        
        # Katakana → Hiragana mapping
        self.kata_to_hira = str.maketrans(
            'ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴヵヶ',
            'ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖ'
        )
    
    def generate(self, text):
        """
        Generate structured ruby format
        
        Args:
            text (str): Japanese text
            
        Returns:
            str: Ruby text in format (kanji-reading)kana
            
        Examples:
            "公園で遊ぶ" → "(公園-こうえん)で(遊-あそ)ぶ"
            "猫" → "(猫-ねこ)"
            "飼われている" → "(飼-か)われている"
        """
        if not text:
            return ""
        
        result = []
        
        for word in self.tagger(text):
            surface = word.surface
            
            # Get reading (katakana → hiragana)
            if hasattr(word.feature, 'kana') and word.feature.kana != '*':
                reading = word.feature.kana.translate(self.kata_to_hira)
            else:
                reading = None
            
            # Check if contains kanji
            has_kanji = bool(re.search(r'[一-龯]', surface))
            
            if not has_kanji:
                # Pure kana - no ruby needed
                result.append(surface)
            else:
                # Contains kanji - process it
                ruby_text = self._process_kanji_word(surface, reading)
                result.append(ruby_text)
        
        return ''.join(result)
    
    def _process_kanji_word(self, surface, reading):
        """Process word containing kanji"""
        
        # Find kanji positions
        kanji_parts = []
        current_pos = 0
        
        for i, char in enumerate(surface):
            if self._is_kanji(char):
                if not kanji_parts or kanji_parts[-1]['end'] != i:
                    # New kanji segment
                    kanji_parts.append({'start': i, 'end': i + 1, 'text': char})
                else:
                    # Continue kanji segment
                    kanji_parts[-1]['end'] = i + 1
                    kanji_parts[-1]['text'] += char
        
        if not kanji_parts:
            return surface
        
        # Build result with ruby
        result = []
        pos = 0
        
        for kanji_part in kanji_parts:
            # Add kana before kanji
            if pos < kanji_part['start']:
                result.append(surface[pos:kanji_part['start']])
            
            # Add kanji with reading
            kanji_text = kanji_part['text']
            kanji_reading = self._extract_kanji_reading(
                surface, reading, kanji_part['start'], kanji_part['end']
            )
            
            if kanji_reading:
                result.append(f"[{kanji_text}|{kanji_reading}]")
            else:
                result.append(kanji_text)
            
            pos = kanji_part['end']
        
        # Add remaining kana
        if pos < len(surface):
            result.append(surface[pos:])
        
        return ''.join(result)
    
    def _extract_kanji_reading(self, surface, reading, start, end):
        """Extract reading for specific kanji part"""
        if not reading:
            return None
        
        # Get okurigana after this kanji part
        okurigana = surface[end:end+10] if end < len(surface) else ""
        
        # Find where okurigana appears in reading
        reading_end = len(reading)
        for i, char in enumerate(okurigana):
            if not self._is_kanji(char):
                pos = reading.find(char)
                if pos != -1:
                    reading_end = pos
                    break
        
        # Calculate reading start based on previous parts
        reading_start = 0
        for i in range(start):
            if not self._is_kanji(surface[i]):
                reading_start += 1
        
        return reading[reading_start:reading_end]
    
    def _is_kanji(self, char):
        """Check if character is kanji"""
        return bool(re.match(r'[一-龯]', char))


# Singleton instance
_ruby_generator = None

def get_ruby_generator():
    """Get or create singleton RubyGenerator instance"""
    global _ruby_generator
    if _ruby_generator is None:
        _ruby_generator = RubyGenerator()
    return _ruby_generator