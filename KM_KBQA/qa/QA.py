import logging
import traceback
from functools import lru_cache

from fuzzywuzzy import fuzz

from ..common import AsyncNeoDriver
from ..common import Segment as seg
from ..config import config
from ..linking import EntityLinking
from .ConstraintExtractor import ConstraintExtractor
from .ListQuestion import check_list_questions
from .RelExtractor import BertRelExtractor, MatchRelExtractor

logger = logging.getLogger('qa')


def load_stopwords(fname):
    with open(fname, 'r', encoding='utf-8') as f:
        stopwords = {line.strip() for line in f}
    return stopwords


stopwords = load_stopwords(config.STOP_WORD_PATH)


def replace_word(sent, stop_list, rpl_list):
    # 过滤词汇
    for stop in stop_list:
        sent = sent.replace(stop, '')
    for replace in rpl_list:
        sent = sent.replace(replace[0], replace[1])
    return sent


class QA():
    stop_list = ['吗', '里面']
    rpl_list = [('在哪儿', '的地点'),
                ('在哪里', '的地点'),
                ('在哪', '的地点'),
                ('哪里', '地点'),
                ('哪里', '地点'),
                ('哪有', '地点'),
                ('属于', '在'),
                ('vip', '贵宾')]

    def __init__(self):
        self.driver = AsyncNeoDriver.get_driver()
        self.bert_linker = EntityLinking.BertLinker()
        self.commercial_linker = EntityLinking.CommercialLinker()
        self.rule_linker = EntityLinking.RuleLinker()
        self.match_extractor = MatchRelExtractor()
        self.bert_extractor = BertRelExtractor()
        self.constr_extractor = ConstraintExtractor()

    def preprocess(self, sent):
        '''
            返回(处理后问句sent_replaced, 分词结果sent_cut, 词性标注 pos_tag)
            问句预处理：
            1. 分词前替换规则
            2. 分词, pos tag
            3. 分词后替换规则：停用词，特殊词
        '''
        # 分词前替换规则
        sent_replaced = replace_word(sent, QA.stop_list, QA.rpl_list)
        # 分词
        sent_cut, pos_tag = seg.pos_cut(sent_replaced)
        # 特殊词处理
        sent_cut_pro = []
        pos_tag_pro = []
        for word, tag in zip(sent_cut, pos_tag):
            if len(word) > 2 and word[-1] == '费':
                sent_cut_pro.append(word[:-1])
                sent_cut_pro.append('费用')
                pos_tag_pro.append(tag)
                pos_tag_pro.append(tag)
            else:
                sent_cut_pro.append(word)
                pos_tag_pro.append(tag)
        # 去除停用词
        sent_cut = []
        pos_tag = []
        for word, tag in zip(sent_cut_pro, pos_tag_pro):
            if word not in stopwords and word != ' ':
                sent_cut.append(word)
                pos_tag.append(tag)

        return sent_replaced, sent_cut, pos_tag

    # @lru_cache(maxsize=128)
    def link(self, sent, sent_cut, pos_tag):
        def merge_link_res(res, id2linked_ent):
            for linked_ent in res:
                neoId = linked_ent['id']
                if neoId in id2linked_ent:
                    if linked_ent['source'] not in id2linked_ent[neoId]['source']:
                        id2linked_ent[neoId]['score'] += linked_ent['score']
                        id2linked_ent[neoId]['source'] += ' ' + \
                            linked_ent['source']
                else:
                    id2linked_ent[neoId] = linked_ent
                    link_res.append(linked_ent)

        bert_res = self.bert_linker.link(sent)
        # logger.info('bert链接: %s' % str(bert_res))
        commercial_res = self.commercial_linker.link(sent, sent_cut)
        # logger.info('商业链接: %s' % str(commercial_res))
        rule_res = self.rule_linker.link(sent, sent_cut, pos_tag)
        # logger.info('规则链接: %s' % str(rule_res))
        link_res = rule_res
        id2linked_ent = {linked_ent['id']: linked_ent
                         for linked_ent in link_res}
        # merge bert res
        merge_link_res(bert_res, id2linked_ent)
        # merge commecial res
        merge_link_res(commercial_res, id2linked_ent)
        # 获取具体实体
        link_res_extend = []
        for linked_ent in link_res:
            ent = linked_ent['ent']
            if (ent['entity_label'] == 'SubGenre' and ent['name'] not in EntityLinking.exception_subgenre)\
                    or ent['entity_label'] == 'Genre':
                if self.rule_linker.is_special_entity(ent):
                    continue
                instances = self.get_instances(
                    ent['name'], ent['entity_label'], 'Instance')
                if instances is not None:
                    link_res_extend.extend([{
                        'ent': e,
                        'mention': linked_ent.get('mention', e['name']),
                        'id':e['neoId'],
                        'score':linked_ent['score'],
                        'source':linked_ent['source']+' sub',
                    } for e in instances])
            elif ent['entity_label'] == 'Instance' or ent['name'] in EntityLinking.exception_subgenre:
                link_res_extend.append(linked_ent)

        link_res_extend.sort(key=lambda x: x['score'], reverse=True)
        id2linked_ent = {
            ent['id']: ent
            for ent in link_res_extend}
        # if ent['ent']['label'] == 'Instance'}
        return link_res_extend, id2linked_ent

    def get_instances(self, parent_name, parent_label, instance_label):
        instances = self.driver.get_genres_by_relation(
            parent_label, instance_label, parent_name, reverse=True).result()
        return instances

    def extract_rel(self, sent: str, sent_cut: list, link_res):
        # 抽取关系
        rel_match_res = []
        for linked_ent in link_res:
            ent = linked_ent['ent']
            # 匹配关系
            rel_matched = self.match_extractor.extract_rel(
                sent_cut, linked_ent)
            rel_match_res += rel_matched
            # bert 提取关系
            rel_bert_res = self.bert_extractor.extract_rel(sent, linked_ent)
            for bert_rel in rel_bert_res:
                for match_rel in rel_matched:
                    if bert_rel['rel_name'] == match_rel['rel_name']:
                        match_rel['rel_score'] += 0.3
                        match_rel['rel_source'] += ' bert'
                    else:
                        rel_match_res.append(bert_rel)
        return rel_match_res

    def match_constraint(self, qa_res, constraint, id2linked_ent):
        non_empty_constr = [(constr_name, constr_val) for constr_name, constr_val in constraint.items()
                            if constr_val != '' and constr_val is not None]
        for ans in qa_res:
            linked_ent = id2linked_ent[ans['id']]
            match_res = None
            try:
                match_res = self.constr_extractor.match_constraint(
                    constraint, linked_ent)
            except:
                print(constraint)
                print(linked_ent)
                traceback.print_exc()

            ans['constr_score'] = 0
            if match_res is not None and len(match_res) != 0:
                # logger.info('限制匹配结果: '+str(match_res))
                for constr, is_match in match_res.items():
                    if is_match:
                        ans['constr_score'] += 0.3
                        ans['constr_name'] = constr
                        ans['constr_val'] = linked_ent['ent'][constr]
                    else:
                        ans['constr_score'] += -0.2
            else:
                for constr_name, constr_val in non_empty_constr:
                    if fuzz.UQRatio(constr_val[0], ans['entity']) >= 60:
                        ans['link_score'] -= 0.3
                    else:
                        ans['constr_score'] += -0.2

    def generate_natural_ans(self, qa_res: dict, id2linked_ent):
        linked_ent = id2linked_ent[qa_res['id']]
        ent = linked_ent['ent']
        ent_name = ent['name']
        cand_name = qa_res['mention']

        natural_ans = ''
        # ans_name = ent_name if fuzz.token_sort_ratio(
        # ' '.join(cand_name), ' '.join(ent_name)) < 80 else cand_name
        ans_name = ent_name

        airline_ans = ''
        if '航司代码' in ent:
            airline_value = ent['航司代码']
            airline_ans = '，办理%s航司业务' % airline_value

        if 'rel_score' not in qa_res:
            ans_list = []
            ans_list.append('您好，机场内有%s' % ans_name)
            # 列举类，找出时间和地点
            for rel, rel_val in ent.items():
                if '时间' in rel:
                    ans_list.append('营业时间是%s' % rel_val)
                if '地点' == rel:
                    ans_list.append('位置在%s' % rel_val)
                if '价格' in rel or '收费' in rel:
                    ans_list.append('价格为%s' % rel_val)
                if '电话' in rel or '联系' in rel:
                    ans_list.append('电话是%s' % rel_val)

            # ans = '您好，机场内有%s，办理%s航司业务，位置在%s，营业时间是%s，价格为%s，电话是%s' % (ans_name, airline_value, loc_prop_value, time_prop_value, price_prop_value, tel_prop_value)

            # 加话术
            # if (time_prop_value == '' or time_prop_value == '暂无') and (loc_prop_value == '' or loc_prop_value == '暂无'):
            #     ans = '您好，机场内有%s' % (ans_name)
            # elif time_prop_value == '' or time_prop_value == '暂无':
            #     ans = '您好，机场内有%s，位置在%s' % (ans_name, loc_prop_value)
            # elif loc_prop_value == '' or loc_prop_value == '暂无':
            #     ans = '您好，机场内有%s，营业时间是%s' % (ans_name, loc_prop_value)
            # else:
            #     ans = '您好，机场内有%s，位置在%s，营业时间是%s' % (ans_name, loc_prop_value, time_prop_value)
            natural_ans = '，'.join(ans_list)
            natural_ans += airline_ans
            return natural_ans
        else:
            rel = qa_res['rel_name']
            rel_val = qa_res['rel_val']
            # 话术生成
            if '地点' in rel:
                natural_ans = '您好，%s在%s' % (ans_name, rel_val)
            elif '时间' in rel:
                natural_ans = '您好，%s的服务时间是%s' % (ans_name, rel_val)
            elif rel in {'客服电话', '联系电话', '联系方式'}:
                natural_ans = '您好，%s的客服电话是%s' % (ans_name, rel_val)
            elif rel == '手续费' or '价格' in rel or '收费' in rel:
                natural_ans = '您好，%s的收费标准是%s' % (ans_name, rel_val)
            else:
                natural_ans = '您好，%s的%s是%s' % (ans_name, rel, rel_val)
            natural_ans += airline_ans
            return natural_ans

    def rank_ans(self, qa_res):
        for ans in qa_res:
            ans['final_score'] = ans.get('constr_score', 0) + \
                ans['link_score'] + \
                ans.get('rel_score', 0)
        qa_res.sort(key=lambda ans: ans['final_score'], reverse=True)
        # qa_res = [ans for ans in qa_res if ans['final_score'] >= 0.6]
        return qa_res

    def answer(self, sent):
        '''
            问答流程：
             1. 使用替换规则修改问句并分词
             2. 规则提取限制
             3. 三种linker提取实体，合并排序
             4. 判断是否列举型
             5. 非列举型匹配关系
             6. 如果没有匹配到的关系，且实体识别分值较高，算作列举
             7. 匹配机场本身相关问题
             8. 匹配限制
             9. 答案排序
             10. 生成自然语言答案
        '''
        logger.info('question :%s' % sent)
        # 1. 使用替换规则(预处理)
        # sent = replace_word(sent, self.stop_list, self.rpl_list)
        # sent_cut = LTP.customed_jieba_cut(sent, cut_stop=True)
        sent, sent_cut, pos_tag = self.preprocess(sent)
        logger.debug('cut :'+str(sent_cut))

        # 2. extract constaints
        constr_res = self.constr_extractor.extract(sent)
        logger.debug('限制: '+str(constr_res))

        # 3. link: 使用多种linker，合并结果
        link_res, id2linked_ent = self.link(sent, sent_cut, pos_tag)
        logger.debug('链接结果: '+str(link_res[:10]))
        # 4. 处理列举类型
        is_list = check_list_questions(sent, link_res)
        # is_list = False
        logger.debug('是否列举: '+str(is_list))
        # 5. 非列举型匹配关系 extract relations, match+bert
        rel_match_res = self.extract_rel(sent, sent_cut, link_res)
        logger.debug('关系匹配: '+str(rel_match_res[:10]))

        # 6. 如果没有匹配到的关系，且实体识别分值较高，算作列举
        # qa_res {'id','mention','entity','link_score','rel_name','rel_val','rel_score','constr_name','constr_val'}
        qa_res = []
        LINK_THRESH = 0.8
        REL_THRESH = 0.8
        match_rel_ent_ids = {e['id'] for e in rel_match_res}
        if is_list:
            qa_res.extend([{
                'id': linked_ent['id'],
                'mention':linked_ent.get('mention', linked_ent['ent']['name']),
                'entity':linked_ent['ent']['name'],
                'link_score':linked_ent['score'],
            } for linked_ent in link_res])
        else:
            # 删掉关系匹配的结果中，匹配结果不高的部分
            qa_res.extend([rel_res for rel_res in rel_match_res
                           if rel_res['rel_score'] >= REL_THRESH and
                           rel_res['link_score'] >= LINK_THRESH
                           ])
            for linked_ent in link_res:
                if linked_ent['id'] not in match_rel_ent_ids \
                        and linked_ent['score'] >= LINK_THRESH:
                    qa_res.append({
                        'id': linked_ent['id'],
                        'mention': linked_ent.get('mention', linked_ent['ent']['name']),
                        'entity': linked_ent['ent']['name'],
                        'link_score': linked_ent['score'],
                    })

        # 7. 匹配机场本身相关的问题
        if len(qa_res) == 0 and '机场' in sent:
            airport_ent = self.driver.get_entities_only_by_name(
                config.airport.name)[0]
            linked_airport = {
                'ent': airport_ent,
                'mention': airport_ent['name'],
                'id': airport_ent['neoId'],
                'score': 1,
                'source': 'airport',
            }
            id2linked_ent[linked_airport['id']] = linked_airport
            airport_rel_match = self.extract_rel(
                sent, sent_cut, [linked_airport])
            qa_res.extend(airport_rel_match)

        # 8. 匹配限制
        if any(map(lambda x: x != '', constr_res.values())):
            self.match_constraint(qa_res, constr_res, id2linked_ent)

        # 9. 答案排序
        qa_res = self.rank_ans(qa_res)
        qa_res = qa_res[:10]
        logger.debug('答案: '+str(qa_res))

        # 10. 生成自然语言答案
        natural_ans = []
        filtered_qa_res = []
        for res in qa_res:
            n_ans = self.generate_natural_ans(
                res, id2linked_ent)
            if n_ans not in natural_ans:
                res['natural_ans'] = n_ans
                natural_ans.append(n_ans)
                filtered_qa_res.append(res)
        logger.info('自然语言答案: '+str(natural_ans))

        return filtered_qa_res


class FrontendAdapter():
    def __init__(self):
        driver = AsyncNeoDriver.get_driver()


def test_qa():
    qa = QA()
    # questions = ['昆明机场是什么时候建立的？', '长水机场在什么地方？', '东航的值机柜台在哪？', '有什么餐厅吗？', '机场哪里有吃饭的地方？', '机场有麦当劳吗？', '打火机可以携带吗？', '机场有婴儿车可以租用吗', '机场有轮椅可以租用吗',
    #              '停车场怎么收费', '停车场费用怎么样？', '停车场一个小时多少钱？', '停车场多少钱？']
    questions = ['过了安检里面有没有书吧？']
    for q in questions:
        qa.answer(q)


if __name__ == "__main__":
    test_qa()
