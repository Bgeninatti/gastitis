import datetime as dt
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.db.models import Sum

from bot.exceptions import ParameterError
from bot.models import TelegramUser, TelegramGroup
from expenses.models import Expense, Tag, ExchangeRate, CURRENCY


def user_and_group(func):
    """
    Add user and group to handler params.
    """
    def wrapper(update, context):
        user_data = update.message.from_user
        chat_id = user_data.id
        first_name = getattr(user_data, 'first_name', chat_id)
        last_name = getattr(user_data, 'last_name') or '-'
        telegram_username = getattr(user_data, 'username', '')
        username = telegram_username or first_name
        user, _ = User.objects.get_or_create(telegram__chat_id=user_data.id, defaults={
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
        })

        TelegramUser.objects.update_or_create(
            user=user, chat_id=chat_id, defaults={
                'username': telegram_username
            })
        group_data = update.message.chat
        group_id = group_data.id
        group_name = group_data.title or username + '__private'

        group, _ = TelegramGroup.objects.get_or_create(chat_id=group_id, defaults={
            'name': group_name,
        })
        group.users.add(user)


        func(update, context, user, group)

    return wrapper


def new_expense(params, user, group):
    """
    Check if params are valid and create a new expense.

    Returns a text to send to the user.
    """
    try:
        data = decode_expense_params(params, group)
    except ParameterError as e:
        return str(e)

    response_text = ''
    amount = data['amount']
    description = data['description']
    date = data['dd']
    tags = data['tt']
    expense = Expense(user=user, group=group, description=description, amount=amount, date=date)
    if data['exchange_rate']:
        exchange_rate = data['exchange_rate']
        expense.original_currency = exchange_rate.currency
        expense.original_amount = data['original_amount']
        response_text += 'Tu gasto se convirtió a {} usando un tipo de cambio = ${} (cargado el ' \
            '{}).\n\n'.format(
                CURRENCY[exchange_rate.currency],
                exchange_rate.rate,
                exchange_rate.date
            )
    expense.save()
    if tags:
        for tag in tags:
            expense.tags.add(tag)

    response_text += 'Se guardó tu gasto {}'.format(expense)
    return response_text


def decode_expense_params(params, group):
    """
    Process command params in expense's attributes, and return a dict with the following data:
    amount = expense amount.
    dd = date or None
    tt = Tag instance or None
    description = string, expense description
    """
    # define special arguments and help texts for them
    special_arguments = {
        'dd': 'Colocar la fecha en la que se generó el gasto después del argumento "dd"',
        'tt': 'Luego de "tt" colocar el nombre de la/las etiqueta/s para el gasto que estás '\
        'cargando. Podés ingresar más de una etiqueta separando los nombres por comas (sin '\
        'espacio).',
    }

    data = {}

    if not params:
        text = 'Necesito que me digas cuanto pagaste y una descripción del gasto.'
        raise ParameterError(text)

    # handle amount
    amount_received, *params = params

    amount, exchange_rate, original_amount = get_amount_and_currency(amount_received)
    data['amount'] = amount
    data['exchange_rate'] = exchange_rate
    data['original_amount'] = original_amount

    #look for special arguments
    for argument, text in special_arguments.items():
        try:
            argument_position = params.index(argument)
            params.pop(argument_position)
            data[argument] = params.pop(argument_position)

        except ValueError:
            data[argument] = None
        except IndexError:
            raise ParameterError(text)

    # handle description
    if not params:
        raise ParameterError('Necesito que agregues en el comando una descripción del gasto')

    data['description'] = ' '.join(params)

    # handle date
    if data['dd']:
        try:
            data['dd'] = dt.datetime.strptime(data['dd'], '%d/%m/%y').date()
        except ValueError:
            text = 'Luego del parámetro "dd" necesito que ingreses la fecha del gasto que estás '\
                   'cargando con formato "dd/mm/yy" (Por ejemplo 28/01/99).'
            raise ParameterError(text)
    else:
        data['dd'] = dt.date.today()

    # handle tags
    if data['tt']:
        tags_list = []
        for t in data['tt'].split(','):
            tag_instnce, _ = Tag.objects.get_or_create(name=data['tt'], group=group)
            tags_list.append(tag_instnce)
        data['tt'] = tags_list

    return data


def get_amount_and_currency(raw_amount):
    """
    Given a string it returns an amount (in the default currency), the original amount  and a
    ExchangeRate instance.  If the string doesn't have a currency specified, it assumes the
    default currency and returns None as ExchangeRate.

    Params:
        - raw_amount = string of an amount (it may have a currency)
    Returns:
        - amount = Decimal number that represent the amount in the default currency.
        - exchange_rate = an exchange rate instance or None if the amount is in the default
        currency.
        - original_amount = the raw amount received, converted in Decimal.
    """
    for key, value in CURRENCY.items():
        if raw_amount.startswith((key, value)) or raw_amount.endswith((key, value)):
            # TODO: get current exchanger rate from api.
            exchange_rate = ExchangeRate.objects.filter(currency=key).last()
            break
    else:
        key, value = ['', '']
        exchange_rate = None
    amount_without_currency = raw_amount.replace(value, '').replace(key, '')

    try:
        original_amount = amount_without_currency.replace(',', '.')
        original_amount = Decimal(original_amount)
    except InvalidOperation:
        text = 'El primer valor que me pasas después del comando tiene que ser el valor de lo '\
               'que pagaste. \n\n También podés especificar un tipo de cambio con el codigo y '\
                ' monto, por ejemplo 40u para 40 dolares (o usd40). \n Los códigos posibles son:'
        for k, v in CURRENCY.items():
            text += '\n - {} ({})'.format(k, v)
            text += '\n - {}'.format(v)

        text += '\n\n El valor "{}" no es un número válido.'.format(amount_without_currency)
        raise ParameterError(text)
    if exchange_rate:
        amount = original_amount * exchange_rate.rate
    else:
        amount = original_amount

    return amount, exchange_rate, original_amount


def show_expenses(group, *params):
    """
    Return a text with expenses processed and filtered according to params.
    """
    group_expenses_qs = Expense.objects.filter(group=group)
    if not group_expenses_qs.exists():
        return "Todavía no hay gastos cargados en este grupo"
    first_expense = group_expenses_qs.last() # first expense is the last one (reverse date order)
    total_expenses = group_expenses_qs.aggregate(Sum('amount'))['amount__sum']
    total_expenses = round(total_expenses, 2)
    user_expenses = {}
    if group.users.count() > 1:
        for user in group.users.all():
            user_expense_qs = group_expenses_qs.filter(user=user)
            user_amount = user_expense_qs.aggregate(Sum('amount'))['amount__sum'] or 0
            user_amount = round(user_amount, 2)
            user_expenses[user.username] = user_amount

    text = "Gastos desde el {} hasta ahora".format(first_expense.date)
    text += "\n\nTotal: ${}\n".format(total_expenses)
    for user, total in user_expenses.items():
        text += "\n\n{}: ${} ({}%)".format(user, total, round(total/total_expenses*100))

    return text
