import logging

from utils.utils.kanji import get_ruby_generator

logger = logging.getLogger(__name__)


def add_ruby(contents, language_code):
    """
    Thêm furigana (khoá "ruby", dạng [漢字|かんじ]かな) vào definition, usage và từng example có nội dung.
    Sinh bằng fugashi nên không tốn AI và luôn khớp với văn bản; lỗi thì bỏ qua, không chặn việc tạo từ.
    """
    nodes = [(contents.get('definition') or {}).get(language_code), (contents.get('usage') or {}).get(language_code)]
    nodes += [ex.get(language_code) for ex in contents.get('examples') or [] if isinstance(ex, dict)]
    for node in nodes:
        if isinstance(node, dict) and node.get('value'):
            try:
                node['ruby'] = get_ruby_generator().generate(node['value'])
            except Exception as e:
                logger.warning('Không sinh được ruby cho %r: %s', node['value'], e)
