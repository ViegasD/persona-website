from app.services.prompt_builder import CharacterLine,VideoPromptInputs,build_video_prompt,character_voice_direction


def test_cat_requires_adult_masculine_voice_without_actor_imitation():
    direction=character_voice_direction('gato-de-botas','masculino')
    prompt=build_video_prompt(VideoPromptInputs('Preserve the reference scene',[CharacterLine('Gato de Botas','Oi, Cacá!',direction)],'',15))
    assert 'adult male Brazilian Portuguese' in prompt
    assert 'never feminine or childlike' in prompt and 'Do not imitate a specific real actor' in prompt
    assert 'Oi, Cacá!' in prompt and '15-second shot' in prompt


def test_multiple_character_voices_stay_associated_with_their_speaking_positions():
    prompt=build_video_prompt(VideoPromptInputs('Preserve the reference scene',[
        CharacterLine('Cinderela','Olá!',character_voice_direction('cinderela','feminino')),
        CharacterLine('Chase','Tudo bem?',character_voice_direction('chase','masculino'))],'',15))
    assert 'Voice for the LEFT speaker (not spoken dialogue): A feminine' in prompt
    assert 'Voice for the RIGHT speaker (not spoken dialogue): A masculine' in prompt
    assert 'Only one character speaks at a time' in prompt


def test_unknown_catalog_gender_does_not_invent_a_voice_profile():
    assert character_voice_direction('unknown',None)==''
    assert character_voice_direction('unknown','unspecified')==''
    assert character_voice_direction('known',' FEMALE ').startswith('A feminine')


def test_legacy_prompt_callers_can_omit_voice_direction():
    prompt=build_video_prompt(VideoPromptInputs('Preserve the scene',[CharacterLine('','Oi!')],'',15))
    assert 'Voice direction (not spoken dialogue)' not in prompt
    assert 'accurate lip sync' in prompt and 'small conversational nods' in prompt
